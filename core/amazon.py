"""
Búsqueda de imágenes de producto en Amazon.es (fuente web_y_amazon).

Amazon sirve cada imagen con un token de tamaño en la URL
(p.ej. `._AC_SX679_`, `._SL1500_`, `._AC_UL320_`). Eliminando ese token se
obtiene la imagen ORIGINAL en su máxima resolución (normalmente 1500px+),
que suele superar a la de muchas webs de fabricante.

Las imágenes de Amazon se combinan con las de la web oficial y el dedup
perceptual (`core.image_utils.dedupe_images`) elige, ante la misma imagen,
la de mayor resolución.

Interfaz pública:
  search_amazon_image_urls(title, barcode="", max_products=3) -> list[str]
"""

import atexit
import json
import logging
import re
import time

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Token de tamaño Amazon: ._AC_SX679_  ._SL1500_  ._AC_UL320_  ._SY450_ ...
_SIZE_TOKEN = re.compile(r'\._[A-Z0-9][A-Z0-9_,]*_\.')
_ASIN_RE    = re.compile(r'/(?:dp|gp/product)/([A-Z0-9]{10})')
_IMG_ID_RE  = re.compile(r'/images/I/([^./]+)')

# Estado Playwright reutilizado entre llamadas
_PW = {"pw": None, "browser": None, "ctx": None, "page": None}


# ─── Utilidades de URL ────────────────────────────────────────────────────────

def strip_size_token(url: str) -> str:
    """Quita el token de tamaño Amazon → URL de la imagen original (máx. resolución)."""
    return _SIZE_TOKEN.sub('.', url.split("?")[0])


def _image_id(url: str) -> str:
    """ID de imagen Amazon (parte tras /images/I/ y antes del primer punto)."""
    m = _IMG_ID_RE.search(url)
    return m.group(1) if m else url


def _is_amazon_product_image(url: str) -> bool:
    return "media-amazon.com/images/I/" in url or "images-amazon.com/images/I/" in url


# ─── Playwright lazy init ─────────────────────────────────────────────────────

def _get_page():
    if _PW["page"] is not None:
        return _PW["page"]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright no instalado para Amazon")
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
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
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

    # Warm-up en amazon.es para establecer cookies
    try:
        page.goto("https://www.amazon.es/", timeout=20000,
                  wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        log.info("  [amazon] warm-up OK")
    except Exception as e:
        log.debug(f"  [amazon] warm-up failed: {e}")

    return page


# ─── Búsqueda DDG de fichas Amazon ────────────────────────────────────────────

def _ddg_amazon_product_urls(title: str, barcode: str = "",
                              max_urls: int = 3) -> list:
    """Busca fichas /dp/{ASIN} en amazon.es vía DDG. Cascada: título → EAN."""
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            log.warning("  [amazon] ddgs no instalado")
            return []

    seen_asin: set = set()
    urls: list = []
    queries = [f"site:amazon.es {title}"]
    if barcode:
        queries.append(f"site:amazon.es {barcode}")

    for q in queries:
        if len(urls) >= max_urls:
            break
        log.info(f"  [amazon DDG] {q}")
        for attempt in range(3):
            try:
                with DDGS() as ddgs:
                    for r in ddgs.text(q, max_results=10):
                        url = r.get("href") or r.get("url") or ""
                        m = _ASIN_RE.search(url)
                        if not m:
                            continue
                        asin = m.group(1)
                        if asin in seen_asin:
                            continue
                        seen_asin.add(asin)
                        urls.append(f"https://www.amazon.es/dp/{asin}")
                        if len(urls) >= max_urls:
                            break
                break
            except Exception as e:
                wait = 3 * (2 ** attempt)
                if attempt < 2:
                    log.warning(f"  [amazon DDG] error ({attempt+1}/3): {e} — reintento {wait}s")
                    time.sleep(wait)
                else:
                    log.warning(f"  [amazon DDG] error final: {e}")

    log.info(f"  [amazon] {len(urls)} fichas: {urls}")
    return urls


# ─── Extracción de imágenes de una ficha ──────────────────────────────────────

def _extract_amazon_images(page) -> list:
    """Extrae URLs de la galería principal y las sube a resolución original."""
    try:
        raw = page.evaluate("""() => {
            const urls = new Set();
            const sel = [
                '#imgTagWrapperId img', '#landingImage',
                '#main-image-container img', '#altImages img',
                'li.imageThumbnail img', '#ivThumbs img',
                '#main-image-container .a-dynamic-image'
            ].join(',');
            document.querySelectorAll(sel).forEach(img => {
                const dyn = img.getAttribute('data-a-dynamic-image');
                if (dyn) {
                    try { Object.keys(JSON.parse(dyn)).forEach(u => urls.add(u)); }
                    catch(e) {}
                }
                const hires = img.getAttribute('data-old-hires');
                if (hires) urls.add(hires);
                if (img.src) urls.add(img.src);
            });
            return Array.from(urls);
        }""")
    except Exception as e:
        log.info(f"  [amazon] error extrayendo DOM: {e}")
        return []

    out: list = []
    seen_ids: set = set()
    for u in (raw or []):
        if not _is_amazon_product_image(u):
            continue
        orig = strip_size_token(u)
        iid = _image_id(orig)
        if iid in seen_ids:
            continue
        seen_ids.add(iid)
        out.append(orig)
    return out


# ─── Interfaz pública ─────────────────────────────────────────────────────────

def search_amazon_image_urls(title: str, barcode: str = "",
                              max_products: int = 3) -> list:
    """
    Devuelve URLs de imágenes de producto Amazon en máxima resolución.
    Busca fichas vía DDG, navega con Playwright y extrae la galería principal.
    """
    product_urls = _ddg_amazon_product_urls(title, barcode=barcode,
                                             max_urls=max_products)
    if not product_urls:
        return []

    page = _get_page()
    if page is None:
        return []

    all_imgs: list = []
    seen_ids: set = set()
    for purl in product_urls:
        try:
            resp = page.goto(purl, timeout=30000, wait_until="domcontentloaded")
            if resp and resp.status >= 400:
                log.info(f"  [amazon] HTTP {resp.status} en {purl}")
                continue
            try:
                page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass
            page.wait_for_timeout(1000)

            imgs = _extract_amazon_images(page)
            log.info(f"  [amazon] {len(imgs)} imgs en {purl}")
            for im in imgs:
                iid = _image_id(im)
                if iid in seen_ids:
                    continue
                seen_ids.add(iid)
                all_imgs.append(im)
        except Exception as e:
            log.info(f"  [amazon] error en {purl}: {e}")

    log.info(f"  [amazon] total {len(all_imgs)} imágenes candidatas")
    return all_imgs
