"""
Scraper para VIRBAC — dos sitios según web_url:
  - store.es.virbac.com → campo 1 (tienda WooCommerce, nombres consumidor)
  - vet-es.virbac.com   → campo 2 (portal veterinario Liferay, acceso público)

Ambos bloquean scrapers directos → Playwright con anti-bot.
Las URLs no son derivables del título → DDG bajo demanda por producto.

vet-es.virbac.com tiene un patrón URL conocido:
  /home/productos/{animal}/{categoria}/{slug}.html

store.es.virbac.com (WooCommerce) tiene un patrón desconocido hasta validar
desde DDG: probablemente /product/{slug}/ o /{animal}/{categoria}/{slug}/.

El campo '_site' del catálogo transporta el sitio activo a find_best_match.

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

_CATALOG_PATHS = {
    "store": Path("resultados/virbac_store_catalog.json"),
    "vet":   Path("resultados/virbac_vet_catalog.json"),
}
MATCH_THRESHOLD = 0.20

IGNORE_TOKENS = {
    "virbac",
    "de", "el", "la", "los", "las", "con", "sin", "y", "para",
    "e", "o", "al", "un", "una", "a",
    "ml", "g", "mg", "mcg", "kg", "gr", "l", "x", "cm",
    "ud", "uds",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_HEADED = (os.getenv("VIRBAC_HEADED") or os.getenv("APPLAWS_HEADED") or "") \
    in ("1", "true", "True")

_PW = {"pw": None, "browser": None, "ctx": None, "page": None, "site": None}


# ─── Utilidades de texto ──────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9\s]", " ", text)


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


def _title_key(title: str) -> str:
    norm = _normalize(title)
    tokens = sorted(
        {t for t in norm.split() if len(t) > 1} - IGNORE_TOKENS
    )
    slug = re.sub(r"\s+", "-", " ".join(tokens)).strip("-")
    return slug if slug else norm.strip()


# ─── Routing de sitio ─────────────────────────────────────────────────────────

def _site_for_web_url(web_url: str) -> str:
    host = urlparse(web_url or "").netloc.lower()
    if "vet-es.virbac.com" in host:
        return "vet"
    return "store"  # store.es.virbac.com o cualquier otro


def _warmup_url(site: str) -> str:
    if site == "vet":
        return "https://vet-es.virbac.com/home/productos.html"
    return "https://store.es.virbac.com/"


# ─── Playwright lazy init ─────────────────────────────────────────────────────

def _get_page(site: str = "store"):
    if _PW["page"] is not None:
        if _PW.get("site") == site:
            return _PW["page"]
        try:
            _PW["browser"].close()
        except Exception:
            pass
        _PW.update({"pw": None, "browser": None, "ctx": None, "page": None, "site": None})

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
    _PW.update({"browser": browser, "ctx": ctx, "page": page, "site": site})

    def _cleanup():
        try:
            browser.close()
        except Exception:
            pass
    atexit.register(_cleanup)

    warmup = _warmup_url(site)
    try:
        page.goto(warmup, timeout=20000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        log.info(f"[warm-up] {warmup} OK")
    except Exception as e:
        log.debug(f"[warm-up] {warmup}: {e}")

    return page


# ─── Filtrado de URLs de producto ─────────────────────────────────────────────

_VET_NON_PRODUCT = re.compile(
    r'^/home/(?!productos/).*|^/home/productos/?$|^/?$', re.IGNORECASE
)
_STORE_NON_PRODUCT = re.compile(
    r'/(carrito|cart|checkout|mi-cuenta|my-account|'
    r'tienda|shop|blog|noticias|contacto|contact|'
    r'sobre-nosotros|legal|privacidad|cookies|'
    r'producto-categoria|product-category)(/|$)',
    re.IGNORECASE
)


def _is_product_url(url: str, site: str = "store") -> bool:
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    path = parsed.path

    if site == "vet":
        if "vet-es.virbac.com" not in netloc:
            return False
        return ("/home/productos/" in path and
                path.lower().rstrip("/").endswith(".html"))

    else:  # store
        if "store.es.virbac.com" not in netloc:
            return False
        if _STORE_NON_PRODUCT.search(path):
            return False
        # Al menos 2 segmentos de path que no sean raíz
        clean = path.strip("/")
        return bool(clean) and clean.count("/") >= 1


# ─── Extracción de imágenes ───────────────────────────────────────────────────

def _should_keep_url(url: str) -> bool:
    low = url.lower()
    return not any(kw in low for kw in (
        ".svg", "logo", "icon", "banner", "sprite", "placeholder",
        "favicon", "loader", "spinner", "flag", "pixel",
    ))


def _extract_images(page, page_url: str) -> list:
    ordered: list = []
    seen: set = set()

    def _add(raw_url: str):
        if not raw_url or raw_url.startswith("data:"):
            return
        full = urljoin(page_url, raw_url)
        if not full.startswith("http") or not _should_keep_url(full):
            return
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
                    if (img.naturalWidth > 0 && img.naturalWidth < 200) return false;
                    return true;
                })
                .map(img => ({
                    srcset:      img.getAttribute('srcset')        || '',
                    dataSrc:     img.getAttribute('data-src')      || '',
                    dataLazy:    img.getAttribute('data-lazy-src') || '',
                    dataFull:    img.getAttribute('data-full-url') || '',
                    src:         img.getAttribute('src')           || ''
                }));
        }""")
        log.info(f"    DOM imgs (excl. relacionados): {len(img_data or [])}")
        for item in (img_data or []):
            srcset = item.get("srcset", "")
            parts = [p.strip() for p in srcset.split(",") if p.strip()]
            if parts:
                _add(parts[-1].split()[0])
            for key in ("dataSrc", "dataLazy", "dataFull", "src"):
                _add(item.get(key, ""))
    except Exception as e:
        log.info(f"    DOM imgs error: {e}")

    log.info(f"    Imágenes extraídas: {len(ordered)}")
    return ordered


def _try_url(page, url: str) -> tuple:
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

def _ddg_query_urls(query: str, max_urls: int, seen: set, site: str) -> list:
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
                    if not _is_product_url(url, site):
                        continue
                    url = url.split("?")[0].split("#")[0].rstrip("/")
                    if site == "vet" and not url.endswith(".html"):
                        url += ".html"
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


def _ddg_find_product_urls(title: str, site: str, max_urls: int = 6) -> list:
    seen: set = set()
    urls: list = []
    domain = "store.es.virbac.com" if site == "store" else "vet-es.virbac.com"

    clean = re.sub(
        r'\b(virbac)\b', '', title, flags=re.IGNORECASE
    ).strip()
    clean = re.sub(r'\s+', ' ', clean).strip()

    q1 = f"site:{domain} {clean}"
    log.info(f"  [DDG] {q1}")
    urls += _ddg_query_urls(q1, max_urls - len(urls), seen, site)

    if len(urls) < 2:
        tokens = [t for t in clean.split() if len(t) > 3][:3]
        if tokens:
            q2 = f"site:{domain} {' '.join(tokens)}"
            if q2 != q1:
                log.info(f"  [DDG fallback] {q2}")
                urls += _ddg_query_urls(q2, max_urls - len(urls), seen, site)

    log.info(f"  [DDG] {len(urls)} candidatos: {urls}")
    return urls


# ─── Persistencia del catálogo ────────────────────────────────────────────────

def _save_catalog(catalog: dict, site: str):
    path = _CATALOG_PATHS.get(site, _CATALOG_PATHS["store"])
    try:
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log.warning(f"No se pudo guardar catálogo Virbac ({site}): {e}")


def save_catalog(catalog: dict):
    site = catalog.get("_site", "store")
    _save_catalog(catalog, site)


# ─── Interfaz pública ─────────────────────────────────────────────────────────

def scrape_catalog(web_url: str, rebuild: bool = False) -> dict:
    """
    Devuelve el catálogo cacheado para el sitio indicado por web_url.
    Guarda '_site' para que find_best_match sepa qué dominio usar.
    """
    site = _site_for_web_url(web_url)
    path = _CATALOG_PATHS[site]
    if rebuild and path.exists():
        path.unlink()
        log.info(f"Catálogo Virbac-{site} borrado (rebuild)")
        return {"_site": site}
    if path.exists():
        try:
            catalog = json.loads(path.read_text(encoding="utf-8"))
            catalog.setdefault("_site", site)
            log.info(f"Catálogo Virbac-{site} cargado: {len(catalog)-1} entradas")
            return catalog
        except Exception:
            pass
    log.info(f"Catálogo Virbac-{site} vacío — resolución bajo demanda vía DDG")
    return {"_site": site}


title_cache_key = _title_key


def scrape_product_url(url: str, barcode: str = "") -> dict | None:
    """Extrae imágenes de una URL de producto Virbac exacta (sin DDG)."""
    site = "vet" if "vet-es.virbac.com" in url else "store"
    page = _get_page(site)
    if page is None:
        return None
    name, images = _try_url(page, url)
    if not images:
        return None
    return {"name": name or "", "url": url, "images": images}


def find_best_match(shopify_title: str, catalog: dict,
                    barcode: str = "") -> tuple:
    """
    1. Cache hit exacto por clave de título.
    2. DDG dirigido al sitio indicado por catalog['_site'].
    3. Jaccard contra h1 de cada candidato.
    Devuelve (handle, score). handle es clave en catalog con la URL real.
    """
    site = catalog.get("_site", "store")
    title_key = _title_key(shopify_title)

    # 1. Cache exacto
    if title_key in catalog and isinstance(catalog[title_key], dict):
        log.info(f"  Match caché: {title_key}")
        return title_key, 1.0

    page = _get_page(site)
    if page is None:
        return None, 0.0

    title_tokens = _tokenize(shopify_title)
    urls = _ddg_find_product_urls(shopify_title, site)
    best = None  # (score, name, images, url)
    for url in urls:
        name, images = _try_url(page, url)
        if not name:
            continue
        score = _similarity(title_tokens, _tokenize(name))
        log.info(f"  [cand] score={score:.2f} '{name}' ({len(images)} imgs) {url}")
        if best is None or score > best[0]:
            best = (score, name, images, url)
        if score >= 0.85:
            break

    if best and best[0] >= MATCH_THRESHOLD:
        score, name, images, url = best
        entry = {"name": name, "url": url, "images": images}
        catalog[title_key] = entry
        _save_catalog(catalog, site)
        log.info(f"  ✓ Resuelto ({site}): '{name}' (score={score:.2f}, {len(images)} imgs)")
        return title_key, score

    if best:
        log.warning(
            f"  Mejor candidato score={best[0]:.2f} < {MATCH_THRESHOLD} "
            f"— descartado: '{best[1]}'"
        )
    log.warning(f"  Sin resolución para '{shopify_title}' (sitio={site})")
    return None, 0.0
