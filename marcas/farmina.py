"""
Scraper para Farmina — farmina.com/es

CMS: PrestaShop con URLs /es/eshop/{categoria}/{subcategoria}/{id}-{slug}.html
Una página web por receta (independientemente del tamaño del paquete).
Los productos Shopify llevan el peso en el título (2.5 KG, 7 KG, 12 KG) → se
elimina antes del matching para lograr correspondencia muchos-a-uno (varios
tamaños de saco → misma URL de receta).

Dos submarcas comparten el mismo dominio:
  - N&D (Natural & Delicious) — vendor "Farmina" en Shopify
  - Vet Life                   — vendor "Farmina Vet Life" en Shopify
Ambas usan la misma caché (resultados/farmina_catalog.json).

Interfaz estándar (requerida por core/process_brand.py):
  scrape_catalog(web_url, rebuild=False) -> dict
  find_best_match(shopify_title, catalog, barcode="") -> (handle, score)
  scrape_product_url(url, barcode="") -> dict | None
"""

import atexit
import json
import logging
import os
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlparse

log = logging.getLogger(__name__)

CATALOG_PATH    = Path("resultados/farmina_catalog.json")
PRODUCT_DOMAIN  = "farmina.com"
MATCH_THRESHOLD = 0.25

# Prefijo de marca y submarca a eliminar del título Shopify antes del matching.
# Cubre variantes: "FARMINA ND", "FARMINA N&D", "FARMINA VET LIFE", "ND", etc.
_BRAND_RE = re.compile(
    r'^(?:FARMINA\s+)?(?:N(?:&|AND)D\s+|ND\s+|VET\s+LIFE\s+|VETLIFE\s+)',
    re.IGNORECASE,
)

# Peso al final o en el interior del título: "2.5 KG", "7 KG", "1,5 KG", "400 G"
_WEIGHT_RE = re.compile(
    r'\b\d+(?:[.,]\d+)?\s*(?:kg|g|gr|lb)\b', re.IGNORECASE
)

IGNORE_TOKENS = {
    "farmina", "nd", "vet", "life", "vetlife",
    "de", "el", "la", "los", "las", "con", "sin", "y", "para",
    "e", "o", "al", "un", "una", "a",
    "kg", "g", "gr", "lb", "ml", "l", "x",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Headed bajo xvfb cuando el workflow lo activa (igual que calibra/menforsan)
_HEADED = (os.getenv("FARMINA_HEADED") or os.getenv("APPLAWS_HEADED") or "") \
    in ("1", "true", "True")

_PW = {"pw": None, "browser": None, "ctx": None, "page": None}


# ─── Utilidades de texto ──────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9\s]", " ", text)


def _strip_weight(title: str) -> str:
    """Elimina tokens de peso del título ('2.5 KG', '400 G', '1,5 KG')."""
    return _WEIGHT_RE.sub("", title).strip()


def _clean_for_match(title: str) -> str:
    """Elimina prefijo de marca y peso. Resultado se usa en matching y DDG."""
    t = _BRAND_RE.sub("", title.strip())
    return _strip_weight(t).strip()


def _title_key(title: str) -> str:
    """Clave de caché estable sin peso → varios tamaños del mismo producto
    comparten la misma entrada y la misma URL de receta.
    Ejemplo: 'FARMINA ND OCEAN PERRO MINI BACALAO 2.5 KG'
             → 'ocean-perro-mini-bacalao'
    """
    norm = _normalize(_clean_for_match(title))
    norm = re.sub(r'\s+', ' ', norm).strip()
    return re.sub(r"\s+", "-", norm).strip("-")


def _tokenize(text: str) -> set:
    tokens = set(_normalize(text).split())
    tokens -= IGNORE_TOKENS
    tokens -= {t for t in tokens if len(t) <= 1}
    return tokens


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _stem_set(tokens: set) -> set:
    return {_stem(t) for t in tokens}


def _similarity(a_tokens: set, b_tokens: set) -> float:
    a = _stem_set(a_tokens)
    b = _stem_set(b_tokens)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ─── Playwright lazy init ─────────────────────────────────────────────────────

def _get_page():
    if _PW["page"] is not None:
        return _PW["page"]
    try:
        from core.playwright_shared import get_playwright
        pw = get_playwright()
    except Exception:
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
        except ImportError:
            log.error("Playwright no instalado: pip install playwright && "
                      "playwright install chromium")
            return None

    browser = pw.chromium.launch(
        headless=not _HEADED,
        args=["--no-sandbox", "--disable-setuid-sandbox",
              "--disable-dev-shm-usage", "--disable-gpu"],
    )
    ctx = browser.new_context(
        user_agent=USER_AGENT,
        locale="es-ES",
        viewport={"width": 1440, "height": 900},
        extra_http_headers={
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        },
        ignore_https_errors=True,
    )
    ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['es-ES', 'es', 'en']});
        window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
    """)
    page = ctx.new_page()
    _PW.update({"browser": browser, "ctx": ctx, "page": page})

    def _cleanup():
        try:
            browser.close()
        except Exception:
            pass
    atexit.register(_cleanup)

    try:
        page.goto("https://www.farmina.com/es/", timeout=20000,
                  wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        status = page.evaluate("() => document.readyState")
        log.info(f"[warm-up] farmina.com/es OK (state={status})")
    except Exception as e:
        log.debug(f"[warm-up] farmina.com/es: {e}")

    return page


# ─── Filtrado de URLs de producto ─────────────────────────────────────────────

def _is_product_url(url: str) -> bool:
    """Acepta solo páginas de producto farmina.com/es/eshop/…/{id}-{slug}.html"""
    if PRODUCT_DOMAIN not in url:
        return False
    path = urlparse(url).path
    if "/es/eshop/" not in path:
        return False
    last = path.rstrip("/").rsplit("/", 1)[-1]
    return bool(re.match(r'^\d+-.+\.html$', last, re.IGNORECASE))


# ─── Extracción de imágenes ───────────────────────────────────────────────────

def _should_keep_url(url: str) -> bool:
    low = url.lower()
    return not any(kw in low for kw in (
        ".svg", "logo", "icon", "banner", "sprite", "placeholder",
        "favicon", "loader", "spinner", "flag", "pixel",
    ))


def _upgrade_prestashop_url(url: str) -> str:
    return re.sub(
        r'/(\d+)-(?:cart|small|medium|home|category)_default/',
        r'/\1-large_default/', url
    )


def _extract_images(page, page_url: str) -> list:
    ordered: list = []
    seen: set = set()

    def _add(raw_url: str):
        if not raw_url or raw_url.startswith("data:"):
            return
        full = urljoin(page_url, raw_url)
        if not full.startswith("http") or not _should_keep_url(full):
            return
        full = _upgrade_prestashop_url(full)
        clean = re.sub(r'-\d+x\d+(\.[a-zA-Z]{3,4})$', r'\1', full.split("?")[0])
        if clean in seen:
            return
        seen.add(clean)
        ordered.append(full)

    try:
        for sel in ("meta[property='og:image']",
                    "meta[property='og:image:secure_url']",
                    "meta[name='twitter:image']"):
            el = page.query_selector(sel)
            if el:
                val = el.get_attribute("content") or ""
                if val:
                    log.info(f"    og:image: {val}")
                _add(val)
    except Exception as e:
        log.info(f"    og:image error: {e}")

    try:
        ld_texts = page.evaluate("""() => {
            return Array.from(
                document.querySelectorAll('script[type="application/ld+json"]'))
                .map(s => s.textContent || '');
        }""")
        for text in (ld_texts or []):
            try:
                data = json.loads(text)
                items = data if isinstance(data, list) else [data]
                if isinstance(data, dict) and "@graph" in data:
                    items = data["@graph"]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    imgs = item.get("image", [])
                    if isinstance(imgs, str):
                        imgs = [imgs]
                    for img in imgs:
                        if isinstance(img, str):
                            _add(img)
                        elif isinstance(img, dict):
                            _add(img.get("url") or img.get("contentUrl") or "")
            except Exception:
                pass
    except Exception:
        pass

    try:
        img_data = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('img'))
                .filter(img => {
                    if (img.closest('.related') || img.closest('.upsells') ||
                        img.closest('.cross-sells') ||
                        img.closest('[class*="related"]') ||
                        img.closest('[class*="upsell"]') ||
                        img.closest('footer') || img.closest('header') ||
                        img.closest('nav')) return false;
                    if (img.closest('.product-miniature') ||
                        img.closest('.js-product-miniature') ||
                        img.closest('[class*="miniature"]') ||
                        img.closest('[class*="product-list"]') ||
                        img.closest('[class*="products-grid"]')) return false;
                    if (img.naturalWidth > 0 && img.naturalWidth < 200) return false;
                    return true;
                })
                .map(img => ({
                    srcset:         img.getAttribute('srcset')           || '',
                    dataSrc:        img.getAttribute('data-src')         || '',
                    dataLazySrc:    img.getAttribute('data-lazy-src')    || '',
                    dataLargeImage: img.getAttribute('data-large_image') || '',
                    dataZoomImage:  img.getAttribute('data-zoom-image')  || '',
                    src:            img.getAttribute('src')              || ''
                }));
        }""")
        log.info(f"    DOM imgs (excl. relacionados): {len(img_data or [])}")
        for item in (img_data or []):
            srcset = item.get("srcset", "")
            parts = [p.strip() for p in srcset.split(",") if p.strip()]
            if parts:
                _add(parts[-1].split()[0])
            for key in ("dataSrc", "dataLazySrc", "dataLargeImage",
                        "dataZoomImage", "src"):
                _add(item.get(key, ""))
    except Exception as e:
        log.info(f"    DOM imgs error: {e}")

    log.info(f"    Imágenes extraídas: {len(ordered)}")
    return ordered


def _try_url(page, url: str) -> tuple:
    """Navega a url y devuelve (h1_name, images). (None, []) si falla."""
    try:
        resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
        if resp and resp.status >= 400:
            log.info(f"  HTTP {resp.status} en {url}")
            return None, []
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)

        name_el = (
            page.query_selector("h1.product_title")
            or page.query_selector("h1.product-title")
            or page.query_selector("h1.product-name")
            or page.query_selector("h1.entry-title")
            or page.query_selector("h1")
        )
        name = name_el.inner_text().strip() if name_el else ""
        if not name:
            log.info(f"  h1 vacío en {url}")
            return None, []
        images = _extract_images(page, page.url)
        return name, images
    except Exception as e:
        log.debug(f"  _try_url error: {e}")
        return None, []


# ─── Búsqueda DDG ─────────────────────────────────────────────────────────────

def _ddg_query_urls(query: str, max_urls: int, seen: set) -> list:
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return []
    urls: list = []
    for attempt in range(3):
        try:
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=10):
                    url = r.get("href") or r.get("url") or ""
                    if not _is_product_url(url):
                        continue
                    url = url.split("?")[0].split("#")[0]
                    if url in seen:
                        continue
                    seen.add(url)
                    urls.append(url)
                    if len(urls) >= max_urls:
                        break
            break
        except Exception as e:
            wait = 3 * (2 ** attempt)
            if attempt < 2:
                log.warning(f"  [DDG] error ({attempt+1}/3): {e} — reintento en {wait}s")
                time.sleep(wait)
            else:
                log.warning(f"  [DDG] error final: {e}")
    return urls


def _ddg_find_product_urls(title: str, max_urls: int = 6) -> list:
    """Cascada DDG: 1) título limpio, 2) versión corta (primeros 4 tokens)."""
    seen: set = set()
    urls: list = []
    clean = _clean_for_match(title)

    q1 = f"site:farmina.com/es/eshop/ {clean}"
    log.info(f"  [DDG] {q1}")
    urls += _ddg_query_urls(q1, max_urls - len(urls), seen)

    if len(urls) < 2:
        short_tokens = _normalize(clean).split()[:4]
        short = " ".join(short_tokens)
        if short and short != _normalize(clean):
            q2 = f"site:farmina.com/es/eshop/ {short}"
            log.info(f"  [DDG fallback] {q2}")
            urls += _ddg_query_urls(q2, max_urls - len(urls), seen)

    log.info(f"  [DDG] {len(urls)} candidatos: {urls}")
    return urls


# ─── Persistencia del catálogo ────────────────────────────────────────────────

def _save_catalog(catalog: dict):
    try:
        CATALOG_PATH.parent.mkdir(exist_ok=True)
        CATALOG_PATH.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log.warning(f"No se pudo guardar catálogo Farmina: {e}")


def save_catalog(catalog: dict):
    _save_catalog(catalog)


# ─── Interfaz pública ─────────────────────────────────────────────────────────

def scrape_catalog(web_url: str, rebuild: bool = False) -> dict:
    """
    Carga el catálogo cacheado. La resolución real es bajo demanda en
    find_best_match() vía DDG. Con rebuild=True borra la caché.
    Soporta vendor 'Farmina' (N&D) y 'Farmina Vet Life' — comparten caché.
    """
    if rebuild and CATALOG_PATH.exists():
        CATALOG_PATH.unlink()
        log.info("Catálogo Farmina borrado (rebuild) — se reconstruirá bajo demanda")
        return {}
    if CATALOG_PATH.exists():
        try:
            catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
            log.info(f"Catálogo Farmina cargado desde caché: {len(catalog)} entradas")
            return catalog
        except Exception:
            pass
    log.info("Catálogo Farmina vacío — resolución bajo demanda vía DDG")
    return {}


# Alias público para backfill de metacampos (mapea título Shopify → clave caché)
title_cache_key = _title_key


def scrape_product_url(url: str, barcode: str = "") -> dict | None:
    """Extrae imágenes de una URL exacta de producto Farmina (sin DDG).
    Habilita el override por metacampo fuentes.url_fabricante."""
    page = _get_page()
    if page is None:
        return None
    name, images = _try_url(page, url)
    if not images:
        return None
    return {"name": name or "", "url": url, "images": images}


def find_best_match(shopify_title: str, catalog: dict,
                    barcode: str = "") -> tuple:
    """
    1. Cache hit exacto por clave de título sin peso.
    2. DDG site:farmina.com/es/eshop/ → candidatos → Jaccard contra h1.
    3. Guarda resultado en catálogo para evitar repetir DDG/navegación.
    Devuelve (handle, score). handle es la clave en catalog con la URL real.

    Nota: varios productos Shopify del mismo tamaño distinto (2.5/7/12 KG)
    de la misma receta comparten la misma clave y la misma URL (many-to-one).
    """
    title_key = _title_key(shopify_title)

    # 1. Cache exacto (incluye resultados de ejecuciones anteriores)
    if title_key in catalog:
        log.info(f"  Match caché: {title_key}")
        return title_key, 1.0

    page = _get_page()
    if page is None:
        return None, 0.0

    # 2. DDG: título limpio (sin marca, sin peso) → candidatos → score Jaccard
    clean_tokens = _tokenize(_clean_for_match(shopify_title))
    urls = _ddg_find_product_urls(shopify_title)
    best = None  # (score, name, images, url)
    for url in urls:
        name, images = _try_url(page, url)
        if not name:
            continue
        score = _similarity(clean_tokens, _tokenize(name))
        log.info(f"  [cand] score={score:.2f} '{name}' ({len(images)} imgs) {url}")
        if best is None or score > best[0]:
            best = (score, name, images, url)
        if score >= 0.85:
            break

    if best and best[0] >= MATCH_THRESHOLD:
        score, name, images, url = best
        entry = {"name": name, "url": url, "images": images}
        catalog[title_key] = entry
        _save_catalog(catalog)
        log.info(f"  ✓ Resuelto: '{name}' (score={score:.2f}, {len(images)} imgs)")
        return title_key, score

    if best:
        log.warning(
            f"  Mejor candidato score={best[0]:.2f} < {MATCH_THRESHOLD} "
            f"— descartado: '{best[1]}'"
        )
    log.warning(f"  Sin resolución para '{shopify_title}'")
    return None, 0.0
