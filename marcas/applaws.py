"""
Scraper para Applaws — applaws.pet (web oficial en español, WooCommerce)

Las páginas de producto usan URLs WooCommerce en español con el peso/tamaño
incluido en el slug, p. ej.:

    https://applaws.pet/producto/applaws-cat-dry-kitten-pollo-2kg/

El peso ("2kg") forma parte del slug pero no siempre está en el título Shopify,
así que el slug no es 100% derivable. La resolución es bajo demanda:
  1. Slug directo desde el título (rápido) — cubre los títulos que ya incluyen
     el peso tal cual aparece en la web.
  2. DuckDuckGo con filtro `site:applaws.pet/producto/` y ranking del h1.

applaws.pet bloquea peticiones sin navegador real (HTTP 403), por lo que toda
la navegación se hace con Playwright + user-agent de Chrome + anti-bot completo.

Interfaz estándar (requerida por core/process_brand.py):
  scrape_catalog(web_url, rebuild=False) -> dict
  find_best_match(shopify_title, catalog, barcode="") -> (handle, score)
  scrape_product_url(url, barcode="") -> {name, url, images} | None   (override metacampo)
  title_cache_key(title) -> str                                       (backfill URLs)
  get_ddg_query(shopify_title) -> str                                 (fallback web_y_amazon)
"""

import atexit
import json
import logging
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlparse

log = logging.getLogger(__name__)

CATALOG_PATH = Path("resultados/applaws_catalog.json")
BASE_URL     = "https://applaws.pet/"
PRODUCT_PATH = "applaws.pet/producto/"
MIN_SCORE    = 0.10
MATCH_THRESHOLD = 0.30

# Notas internas que la tienda añade a los títulos: *DX*, (NDR), (PV)...
_TITLE_NOISE = re.compile(r'\s*(?:\*[^*]*\*|\((?:NDR|PV|NV|ONLINE)\))\s*', re.IGNORECASE)

IGNORE_TOKENS = {
    "applaws",
    "de", "el", "la", "los", "las", "con", "sin", "y", "e", "o", "a", "para",
    "un", "una", "al", "del",
    "ml", "gr", "g", "kg", "mg", "cl", "l", "cm", "mm", "x", "ud", "uds", "pack",
    "dx",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Estado Playwright reutilizado entre llamadas
_PW = {"pw": None, "browser": None, "ctx": None, "page": None, "warmed": False}


# ─── Utilidades de texto ──────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    # "2kg" → "2 kg" para que la unidad caiga como stopword y quede el número
    text = re.sub(r"(\d+)\s*(ml|gr|kg|mg|cl|l|cm|mm|g)\b", r"\1 \2", text)
    return text


def _clean_title(title: str) -> str:
    return _TITLE_NOISE.sub(" ", title).strip()


def _title_key(title: str) -> str:
    """Clave de caché estable derivada del título Shopify."""
    norm = _normalize(_clean_title(title))
    return re.sub(r"\s+", "-", norm.strip()).strip("-")


def _direct_slug(title: str) -> str:
    """Slug WooCommerce directo desde el título: 'applaws-...-2kg'."""
    norm = unicodedata.normalize("NFD", _clean_title(title).lower())
    ascii_str = norm.encode("ascii", "ignore").decode()
    slug = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", ascii_str)).strip("-")
    if slug and not slug.startswith("applaws"):
        slug = "applaws-" + slug
    return slug


def _tokenize(text: str) -> set:
    tokens = set(_normalize(text).split())
    tokens -= IGNORE_TOKENS
    tokens -= {t for t in tokens if len(t) <= 1}
    return tokens


def _stem(token: str) -> str:
    """Stemming mínimo de plurales españoles: 'gatos'→'gato'."""
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _stem_set(tokens: set) -> set:
    return {_stem(t) for t in tokens}


def _similarity(title_tokens: set, name_tokens: set) -> float:
    a = _stem_set(title_tokens)
    b = _stem_set(name_tokens)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# Alias público para --backfill-urls
title_cache_key = _title_key


# ─── Playwright lazy init (anti-bot 403 + warm-up) ──────────────────────────────

def _get_page():
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
        extra_http_headers={
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
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
    _PW.update({"pw": pw, "browser": browser, "ctx": ctx, "page": page})

    def _cleanup():
        try:
            browser.close()
            pw.stop()
        except Exception:
            pass
    atexit.register(_cleanup)
    return page


def _warm_up(page):
    """Visita la homepage para establecer cookies antes de navegar a productos."""
    if _PW["warmed"]:
        return
    try:
        page.goto(BASE_URL, timeout=20000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
    except Exception as e:
        log.info(f"  [warm-up] {e}")
    _PW["warmed"] = True


# ─── Filtrado / normalización de URLs de imagen ─────────────────────────────────

def _should_keep_url(url: str) -> bool:
    low = url.lower()
    if any(kw in low for kw in (".svg", "logo", "icon", "banner", "sprite",
                                 "placeholder", "favicon", "/static/",
                                 "loader", "spinner", "flag", "pixel",
                                 "woocommerce-placeholder")):
        return False
    return True


def _strip_size_suffix(url: str) -> str:
    """WordPress añade '-300x300' antes de la extensión en los thumbnails."""
    return re.sub(r'-\d+x\d+(\.[a-zA-Z]{3,4})(?:\?.*)?$', r'\1', url.split("?")[0])


# ─── Extracción de imágenes (WooCommerce) ───────────────────────────────────────

def _extract_images(page, page_url: str) -> list:
    ordered: list = []
    seen: set = set()

    def _add(raw_url: str):
        if not raw_url or raw_url.startswith("data:"):
            return
        full = urljoin(page_url, raw_url)
        if not full.startswith("http"):
            return
        if not _should_keep_url(full):
            return
        clean = _strip_size_suffix(full)
        if clean in seen:
            return
        seen.add(clean)
        ordered.append(clean)

    # 1. og:image
    try:
        for sel in ("meta[property='og:image']",
                    "meta[property='og:image:secure_url']",
                    "meta[name='twitter:image']"):
            el = page.query_selector(sel)
            if el:
                _add(el.get_attribute("content") or "")
    except Exception:
        pass

    # 2. JSON-LD (schema.org Product)
    try:
        ld_texts = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
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

    # 3. <img> de la galería del producto (excluye relacionados/upsells)
    try:
        img_data = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('img'))
                .filter(img =>
                    !img.closest('.related') &&
                    !img.closest('.upsells') &&
                    !img.closest('.cross-sells') &&
                    !img.closest('[class*="related-product"]') &&
                    !img.closest('[id*="related"]') &&
                    !img.closest('footer') &&
                    !img.closest('header') &&
                    !img.closest('nav')
                )
                .map(img => ({
                    srcset:         img.getAttribute('srcset')           || '',
                    dataSrc:        img.getAttribute('data-src')         || '',
                    dataLazySrc:    img.getAttribute('data-lazy-src')    || '',
                    dataLargeImage: img.getAttribute('data-large_image') || '',
                    dataZoomImage:  img.getAttribute('data-zoom-image')  || '',
                    src:            img.getAttribute('src')              || ''
                }));
        }""")
        for item in (img_data or []):
            srcset = item.get("srcset", "")
            parts  = [p.strip() for p in srcset.split(",") if p.strip()]
            if parts:
                _add(parts[-1].split()[0])
            for key in ("dataSrc", "dataLazySrc", "dataLargeImage",
                        "dataZoomImage", "src"):
                _add(item.get(key, ""))
    except Exception:
        pass

    log.info(f"    Imágenes extraídas: {len(ordered)}")
    return ordered


def _fetch_name(page, url: str) -> str:
    """Navega a la URL y devuelve el h1 del producto. '' si no es válida (404)."""
    try:
        resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
        if resp and resp.status >= 400:
            log.info(f"  HTTP {resp.status} en {url}")
            return ""
        name_el = (
            page.query_selector("h1.product_title")
            or page.query_selector("h1.product-title")
            or page.query_selector("h1.entry-title")
            or page.query_selector("h1")
        )
        return name_el.inner_text().strip() if name_el else ""
    except Exception as e:
        log.info(f"  _fetch_name error en {url}: {e}")
        return ""


# ─── Búsqueda DDG con filtro site: ──────────────────────────────────────────────

def get_ddg_query(title: str) -> str:
    return f"applaws {_clean_title(title)}"


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
                    if PRODUCT_PATH not in url:
                        continue
                    url = url.split("?")[0].split("#")[0].rstrip("/") + "/"
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


def _ddg_find_product_urls(title: str, barcode: str = "",
                           max_urls: int = 6) -> list:
    """URLs candidatas de applaws.pet/producto/. Cascada: título completo →
    sin prefijo 'APPLAWS' → por EAN."""
    seen: set = set()
    urls: list = []
    clean = _clean_title(title)

    q1 = f"site:applaws.pet/producto/ {clean}"
    log.info(f"  [DDG] {q1}")
    urls += _ddg_query_urls(q1, max_urls - len(urls), seen)

    if len(urls) < max_urls:
        no_brand = re.sub(r'^APPLAWS\s+', '', clean, flags=re.IGNORECASE).strip()
        if no_brand != clean:
            q2 = f"site:applaws.pet/producto/ {no_brand}"
            log.info(f"  [DDG sin marca] {q2}")
            urls += _ddg_query_urls(q2, max_urls - len(urls), seen)

    if len(urls) < 2 and barcode:
        q3 = f"site:applaws.pet/producto/ {barcode}"
        log.info(f"  [DDG EAN] {q3}")
        urls += _ddg_query_urls(q3, max_urls - len(urls), seen)

    log.info(f"  [DDG] {len(urls)} candidatos: {urls}")
    return urls


# ─── Persistencia del catálogo ──────────────────────────────────────────────────

def _save_catalog(catalog: dict):
    try:
        CATALOG_PATH.parent.mkdir(exist_ok=True)
        CATALOG_PATH.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log.warning(f"No se pudo guardar catálogo: {e}")


save_catalog = _save_catalog


# ─── Interfaz pública ────────────────────────────────────────────────────────────

def scrape_catalog(web_url: str, rebuild: bool = False) -> dict:
    """Carga el catálogo cacheado. Cada producto se resuelve bajo demanda en
    find_best_match() (slug directo + DDG), así que aquí solo se gestiona caché."""
    if rebuild and CATALOG_PATH.exists():
        CATALOG_PATH.unlink()
        log.info("Catálogo borrado (rebuild) — se reconstruirá bajo demanda")
        return {}
    if CATALOG_PATH.exists():
        try:
            catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
            log.info(f"Catálogo cargado desde caché: {len(catalog)} entradas")
            return catalog
        except Exception:
            pass
    log.info("Catálogo vacío — se rellenará bajo demanda")
    return {}


def scrape_product_url(url: str, barcode: str = "") -> dict | None:
    """Extrae imágenes de una URL exacta (override metacampo fuentes.url_fabricante)."""
    page = _get_page()
    if page is None:
        return None
    _warm_up(page)
    try:
        resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
        if resp and resp.status >= 400:
            log.info(f"  HTTP {resp.status} en {url}")
            return None
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)
        name_el = (page.query_selector("h1.product_title")
                   or page.query_selector("h1.entry-title")
                   or page.query_selector("h1"))
        name = name_el.inner_text().strip() if name_el else ""
        images = _extract_images(page, page.url)
        if not images:
            return None
        return {"name": name, "url": url, "images": images}
    except Exception as e:
        log.warning(f"  scrape_product_url error en {url}: {e}")
        return None


def find_best_match(shopify_title: str, catalog: dict,
                    barcode: str = "") -> tuple:
    """
    Resolución por producto:
      1. Cache hit por clave de título normalizada.
      2. Slug directo (rápido): applaws.pet/producto/<slug>/ desde el título.
      3. DDG site:applaws.pet/producto/ → ranking del h1 por similitud.
    Extrae las imágenes solo del candidato ganador. Devuelve (handle, score).
    """
    title_tokens = _tokenize(shopify_title)
    title_key = _title_key(shopify_title)

    if title_key in catalog and catalog[title_key].get("url"):
        log.info(f"  Match caché: {title_key}")
        return title_key, 1.0

    page = _get_page()
    if page is None:
        return None, 0.0
    _warm_up(page)

    candidates: list = []   # (url, name)
    seen: set = set()

    # 2. Slug directo
    direct = _direct_slug(shopify_title)
    if direct:
        durl = f"{BASE_URL}producto/{direct}/"
        name = _fetch_name(page, durl)
        if name:
            candidates.append((durl, name))
            seen.add(durl)
            log.info(f"  [slug directo] {durl} → '{name}'")

    # 3. DDG (siempre, para cubrir el peso desconocido en el slug)
    for url in _ddg_find_product_urls(shopify_title, barcode=barcode):
        if url in seen:
            continue
        name = _fetch_name(page, url)
        if name:
            candidates.append((url, name))
            seen.add(url)

    # Ranking por similitud del h1
    best = None   # (score, url, name)
    for url, name in candidates:
        score = _similarity(title_tokens, _tokenize(name))
        log.info(f"  [cand] score={score:.2f} '{name}' {url}")
        if best is None or score > best[0]:
            best = (score, url, name)

    if best and best[0] >= MATCH_THRESHOLD:
        score, url, name = best
        log.info(f"  Extrayendo imágenes del ganador: {url}")
        entry = scrape_product_url(url, barcode=barcode) or {
            "name": name, "url": url, "images": []}
        entry.setdefault("name", name)
        entry["url"] = url
        catalog[title_key] = entry
        slug = url.rstrip("/").split("/")[-1]
        if slug and slug != title_key:
            catalog[slug] = entry
        _save_catalog(catalog)
        log.info(f"  ✓ Resuelto: {name} (score={score:.2f}, "
                 f"{len(entry.get('images', []))} imgs) {url}")
        return title_key, score

    if best:
        log.warning(f"  Mejor candidato score={best[0]:.2f} < {MATCH_THRESHOLD} "
                    f"— descartado: '{best[2]}'")
    log.warning(f"  Sin resolución para '{shopify_title}'")
    return None, 0.0
