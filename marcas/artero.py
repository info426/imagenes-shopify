"""
Scraper para Artero — artero.com/es/petcare/

El listado se carga vía JS y no es fiable de scrapear. En su lugar usamos
una estrategia híbrida que busca la URL de cada producto bajo demanda:

  1. Match Jaccard contra el catálogo ya cacheado
  2. Construir slug a partir del título Shopify y probar la URL directa
     (ARTERO ACONDICIONADOR FLASH 300 ML → artero-acondicionador-flash-300ml)
  3. Fallback: DuckDuckGo con filtro site:artero.com/es/petcare/
  4. Cuando se encuentra una URL válida → cachear en resultados/artero_catalog.json

Interfaz estándar (requerida por core/process_brand.py):
  scrape_catalog(web_url, rebuild=False) -> dict
  find_best_match(shopify_title, catalog) -> (handle, score)
"""

import atexit
import json
import logging
import re
import time
import unicodedata
from pathlib import Path

log = logging.getLogger(__name__)

CATALOG_PATH = Path("resultados/artero_catalog.json")
BASE_URL     = "https://artero.com/es/petcare"
BASE_DOMAIN  = "artero.com"
MIN_SCORE    = 0.10

# Marcadores internos que añade la tienda a los títulos
_TITLE_NOISE = re.compile(r'\s*\((?:NDR|PV|NV|ONLINE)\)\s*', re.IGNORECASE)

IGNORE_TOKENS = {
    "artero", "professional", "pet",
    "de", "el", "la", "los", "las", "con", "sin", "y", "para",
    "ml", "gr", "g", "kg", "l", "x", "oz", "ud", "uds",
    "ndr", "pv", "nv", "online",
    "a", "e", "o",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Estado Playwright reutilizado entre llamadas a find_best_match()
_PW = {"pw": None, "browser": None, "ctx": None, "page": None}


# ─── Utilidades de texto ──────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9\s]", " ", text)


def _clean_title(title: str) -> str:
    return _TITLE_NOISE.sub(" ", title).strip()


def _title_to_slug(title: str) -> str:
    """ARTERO ACONDICIONADOR FLASH 300 ML (NDR) → artero-acondicionador-flash-300ml"""
    norm = _normalize(_clean_title(title))
    # "300 ml" → "300ml" (la web concatena número y unidad)
    norm = re.sub(r'(\d+)\s+(ml|gr|kg|g|l|oz)\b', r'\1\2', norm)
    slug = re.sub(r'\s+', '-', norm.strip())
    return slug.strip('-')


def _tokenize(text: str) -> set:
    tokens = set(_normalize(text).split())
    tokens -= IGNORE_TOKENS
    tokens -= {t for t in tokens if len(t) <= 1}
    return tokens


def _jaccard(a: set, b: set) -> float:
    u = a | b
    return len(a & b) / len(u) if u else 0.0


# ─── Playwright lazy init ─────────────────────────────────────────────────────

def _get_page():
    """Inicializa Playwright en el primer uso. Se cierra al final del proceso."""
    if _PW["page"] is not None:
        return _PW["page"]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright no instalado: pip install playwright && "
                  "playwright install chromium")
        return None

    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox",
              "--disable-dev-shm-usage", "--disable-gpu"],
    )
    ctx = browser.new_context(
        user_agent=USER_AGENT,
        locale="es-ES",
        viewport={"width": 1440, "height": 900},
        extra_http_headers={"Accept-Language": "es-ES,es;q=0.9,en;q=0.8"},
        ignore_https_errors=True,
    )
    page = ctx.new_page()
    _PW.update({"pw": pw, "browser": browser, "ctx": ctx, "page": page})

    def _cleanup():
        try:
            browser.close()
            pw.stop()
        except Exception:
            pass
    atexit.register(_cleanup)
    return page


# ─── Extracción de imágenes ──────────────────────────────────────────────────

def _extract_images(page) -> list:
    urls: set = set()

    # 1. JSON-LD (más fiable)
    try:
        ld_texts = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
                        .map(s => s.textContent || '');
        }""")
        for text in (ld_texts or []):
            try:
                data = json.loads(text)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    imgs = item.get("image", [])
                    if isinstance(imgs, str):
                        imgs = [imgs]
                    for img in imgs:
                        if isinstance(img, str) and img.startswith("http"):
                            urls.add(img.split("?")[0])
                        elif isinstance(img, dict):
                            u = img.get("url") or img.get("contentUrl") or ""
                            if u.startswith("http"):
                                urls.add(u.split("?")[0])
            except Exception:
                pass
    except Exception:
        pass

    # 2. srcset (mayor resolución)
    try:
        for el in page.query_selector_all("img[srcset]"):
            srcset = el.get_attribute("srcset") or ""
            parts  = [p.strip() for p in srcset.split(",") if p.strip()]
            if parts:
                cand = parts[-1].split()[0]
                if cand and not cand.startswith("data:"):
                    full = cand if cand.startswith("http") else f"https:{cand}"
                    if BASE_DOMAIN in full:
                        urls.add(re.sub(r'-\d+x\d+\.', '.', full).split("?")[0])
    except Exception:
        pass

    # 3. <img src>
    try:
        for el in page.query_selector_all("img[src]"):
            src = el.get_attribute("src") or ""
            if not src or src.startswith("data:"):
                continue
            full = src if src.startswith("http") else (
                f"https:{src}" if src.startswith("//") else ""
            )
            if not full or BASE_DOMAIN not in full:
                continue
            low = full.lower()
            if any(kw in low for kw in (".svg", "logo", "icon", "banner",
                                         "sprite", "placeholder", "favicon")):
                continue
            urls.add(re.sub(r'-\d+x\d+\.', '.', full).split("?")[0])
    except Exception:
        pass

    # 4. data-src / data-zoom-image
    for attr in ("data-src", "data-lazy-src", "data-zoom-image",
                 "data-large-image", "data-full-url"):
        try:
            for el in page.query_selector_all(f"img[{attr}]"):
                src = el.get_attribute(attr) or ""
                if src and not src.startswith("data:"):
                    full = src if src.startswith("http") else f"https:{src}"
                    if BASE_DOMAIN in full:
                        urls.add(re.sub(r'-\d+x\d+\.', '.', full).split("?")[0])
        except Exception:
            pass

    return list(urls)


def _try_url(page, url: str, title_tokens: set) -> tuple:
    """
    Visita la URL y devuelve (name, images) si es una página de producto válida.
    Si la página no existe, no es producto, o no comparte tokens con el título,
    devuelve (None, []).
    """
    try:
        resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
        if resp and resp.status >= 400:
            log.debug(f"  HTTP {resp.status} en {url}")
            return None, []
        page.wait_for_timeout(2000)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)

        name_el = (
            page.query_selector("h1.product_title")
            or page.query_selector("h1.product-title")
            or page.query_selector("h1.entry-title")
            or page.query_selector("h1")
        )
        name = name_el.inner_text().strip() if name_el else ""
        if not name:
            return None, []

        # Sanity check: el h1 debe compartir tokens con el título Shopify
        name_tokens = _tokenize(name)
        if title_tokens and not (title_tokens & name_tokens):
            log.debug(f"  Sin overlap de tokens: '{name}'")
            return None, []

        images = _extract_images(page)
        return name, images
    except Exception as e:
        log.debug(f"  _try_url error: {e}")
        return None, []


# ─── Búsqueda DDG con filtro site: ────────────────────────────────────────────

def _ddg_find_product_url(title: str) -> str | None:
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            log.warning("  [DDG] ddgs no instalado")
            return None

    clean = _clean_title(title)
    query = f"site:artero.com/es/petcare/ {clean}"
    log.info(f"  [DDG] {query}")
    for attempt in range(3):
        try:
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=10):
                    url = r.get("href") or r.get("url") or ""
                    if "artero.com/es/petcare/" not in url:
                        continue
                    url = url.split("?")[0].split("#")[0].rstrip("/")
                    # Excluir la propia categoría
                    if url.rstrip("/").endswith("/petcare"):
                        continue
                    log.info(f"  [DDG] → {url}")
                    return url
            break
        except Exception as e:
            wait = 3 * (2 ** attempt)
            if attempt < 2:
                log.warning(f"  [DDG] error ({attempt+1}/3): {e} — reintento en {wait}s")
                time.sleep(wait)
            else:
                log.warning(f"  [DDG] error final: {e}")
    return None


# ─── Persistencia del catálogo ────────────────────────────────────────────────

def _save_catalog(catalog: dict):
    try:
        CATALOG_PATH.parent.mkdir(exist_ok=True)
        CATALOG_PATH.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log.warning(f"No se pudo guardar catálogo: {e}")


# ─── Interfaz pública ─────────────────────────────────────────────────────────

def scrape_catalog(web_url: str, rebuild: bool = False) -> dict:
    """
    Carga el catálogo cacheado. NO realiza scraping bulk del listado.
    El catálogo se rellena bajo demanda en find_best_match().
    Con rebuild=True se borra el caché para forzar nueva resolución de cada producto.
    """
    if rebuild and CATALOG_PATH.exists():
        CATALOG_PATH.unlink()
        log.info("Catálogo borrado (rebuild) — se reconstruirá bajo demanda")
        return {}
    if CATALOG_PATH.exists():
        try:
            catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
            log.info(f"Catálogo cargado desde caché: {len(catalog)} productos")
            return catalog
        except Exception:
            pass
    log.info("Catálogo vacío — se rellenará bajo demanda")
    return {}


def find_best_match(shopify_title: str, catalog: dict) -> tuple:
    """
    Estrategia híbrida:
      1. Jaccard contra catálogo cacheado
      2. Si no hay match: slug directo (ARTERO <X> 300 ML → artero-<x>-300ml)
      3. Si la URL directa falla: DDG site:artero.com/es/petcare/
      4. URL válida → fetch + extracción → entry añadida al catálogo (mutación)
    """
    title_tokens = _tokenize(shopify_title)

    # 1. Match en catálogo cacheado
    best_handle, best_score = None, 0.0
    for handle, entry in catalog.items():
        cat_tokens = (_tokenize(handle.replace("-", " "))
                      | _tokenize(entry.get("name", "")))
        score = _jaccard(title_tokens, cat_tokens)
        if score > best_score:
            best_score, best_handle = score, handle

    if best_score >= MIN_SCORE:
        log.info(f"  Match caché: {best_handle} (score={best_score:.2f})")
        return best_handle, best_score

    log.info(f"  Sin match en caché — búsqueda directa de URL")

    page = _get_page()
    if page is None:
        return None, best_score

    # 2. Slug directo
    slug = _title_to_slug(shopify_title)
    direct_url = f"{BASE_URL}/{slug}"
    log.info(f"  [slug] {direct_url}")
    name, images = _try_url(page, direct_url, title_tokens)
    found_url = direct_url if name else None

    # 3. Fallback DDG
    if not name:
        ddg_url = _ddg_find_product_url(shopify_title)
        if ddg_url:
            name, images = _try_url(page, ddg_url, title_tokens)
            if name:
                slug = ddg_url.rstrip("/").split("/")[-1]
                found_url = ddg_url

    if not name or not found_url:
        return None, best_score

    # 4. Cachear y devolver match
    catalog[slug] = {"name": name, "url": found_url, "images": images}
    _save_catalog(catalog)
    log.info(f"  ✓ Resuelto: {slug} ({len(images)} imágenes)")
    return slug, 1.0
