"""
Búsqueda de imágenes de producto en Amazon.es (fuente web_y_amazon).

ESTRATEGIA PRINCIPAL — DuckDuckGo Image Search (sin CAPTCHA):
  En lugar de navegar la página /dp/{ASIN} (que Amazon protege con CAPTCHA
  desde IPs de datacenter como las de GitHub Actions), usamos la búsqueda de
  IMÁGENES de DDG restringida a amazon.es. DDG devuelve URLs directas del CDN
  de Amazon (m.media-amazon.com/images/I/...), que NO está protegido. Quitando
  el token de tamaño de cada URL (._AC_SL1500_ → original) se obtiene la imagen
  en máxima resolución. Funciona desde cualquier IP.

ESTRATEGIA SECUNDARIA — Playwright (opt-in con AMAZON_USE_PLAYWRIGHT=1):
  Navega la página de producto y extrae la galería completa vía colorImages
  (clave 'hiRes'). Solo fiable desde IP residencial (uso local); en GitHub
  Actions normalmente da CAPTCHA. Se usa como fallback cuando DDG image search
  no devuelve nada.

Las imágenes se combinan con las de la web oficial y el dedup perceptual
(core.image_utils.dedupe_images) elige, ante la misma imagen, la de mayor
resolución; las imágenes únicas de Amazon enriquecen el carrusel.

Interfaz pública:
  search_amazon_image_urls(title, barcode="", max_products=3) -> list[str]
"""

import atexit
import json
import logging
import os
import re
import time
import unicodedata

log = logging.getLogger(__name__)

# ─── Matching de título ───────────────────────────────────────────────────────

_STOPWORDS = {
    "de", "el", "la", "los", "las", "con", "sin", "y", "e", "o", "a", "para",
    "un", "una", "al", "en", "por", "su", "se", "que",
    "ml", "gr", "g", "kg", "l", "cm", "mm", "x", "ud", "uds",
    "the", "for", "and", "or", "of", "with",
}
# Similitud mínima entre el título Shopify y el título de la página/imagen Amazon.
# Descarta productos de la misma marca pero distinta referencia
# (ej. "Champú Intensificador de Color" ≠ "Champú de Biotina").
AMAZON_MATCH_THRESHOLD = 0.40


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9\s]", " ", text)


def _tok(text: str) -> set:
    tokens = set(_norm(text).split())
    tokens -= _STOPWORDS
    return {t for t in tokens if len(t) > 1}


def _title_sim(a: str, b: str) -> float:
    ta, tb = _tok(a), _tok(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Token de tamaño Amazon: ._AC_SX679_  ._SL1500_  ._AC_UL320_  ._SY450_ ...
_SIZE_TOKEN = re.compile(r'\._[A-Z0-9][A-Z0-9_,]*_\.')
_ASIN_RE    = re.compile(r'/(?:dp|gp/product)/([A-Z0-9]{10})')
_IMG_ID_RE  = re.compile(r'/images/I/([^./]+)')

# Estado Playwright (solo para el fallback opt-in)
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


def _is_ui_sprite(url: str) -> bool:
    """Descarta iconos/overlays de UI de Amazon (play-icon, sprites, etc.)."""
    low = url.lower()
    return any(kw in low for kw in (
        "play-icon", "overlay", "sprite", "gno/", "/x-locale/",
        "transparent-pixel", "grey-pixel",
    ))


# ─── Método principal: DDG image search ───────────────────────────────────────

def _ddgs():
    try:
        from ddgs import DDGS
        return DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
            return DDGS
        except ImportError:
            log.warning("  [amazon] ddgs no instalado")
            return None


def _search_via_ddg_images(title: str, barcode: str = "") -> list:
    """
    Busca imágenes de producto Amazon vía DDG image search.

    No navega la página de producto → no hay CAPTCHA posible. Filtra a imágenes
    alojadas en el CDN de Amazon, quita el token de tamaño (→ original) y
    deduplica por ID de imagen. Cascada: título → EAN.
    """
    DDGS = _ddgs()
    if DDGS is None:
        return []

    queries = [f"site:amazon.es {title}"]
    if barcode:
        queries.append(f"site:amazon.es {barcode}")

    seen_ids: set = set()
    out: list = []
    for q in queries:
        log.info(f"  [amazon img] {q}")
        hits: list = []
        for attempt in range(3):
            try:
                with DDGS() as ddgs:
                    # size="Large" sesga hacia imágenes de alta resolución
                    hits = list(ddgs.images(q, max_results=60, size="Large"))
                break
            except Exception as e:
                wait = 3 * (2 ** attempt)
                if attempt < 2:
                    log.warning(f"  [amazon img] error ({attempt+1}/3): {e} "
                                f"— reintento {wait}s")
                    time.sleep(wait)
                else:
                    log.warning(f"  [amazon img] error final: {e}")

        for r in hits:
            img_url = r.get("image") or r.get("thumbnail") or ""
            if not _is_amazon_product_image(img_url) or _is_ui_sprite(img_url):
                continue
            orig = strip_size_token(img_url)
            iid = _image_id(orig)
            if iid in seen_ids:
                continue
            seen_ids.add(iid)
            out.append(orig)
            log.info(f"  [amazon img] + {iid}  «{(r.get('title') or '')[:55]}»")

        if out:
            break  # la primera query con resultados basta; el EAN es respaldo

    log.info(f"  [amazon] {len(out)} imágenes vía DDG image search")
    return out


# ─── Fallback opt-in: Playwright (navegación de página de producto) ────────────

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
            log.error("Playwright no instalado para Amazon")
            return None

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
    _PW.update({"browser": browser, "ctx": ctx, "page": page})

    def _cleanup():
        try:
            browser.close()
        except Exception:
            pass
    atexit.register(_cleanup)

    try:
        page.goto("https://www.amazon.es/", timeout=20000,
                  wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        log.info("  [amazon] warm-up OK")
    except Exception as e:
        log.debug(f"  [amazon] warm-up failed: {e}")

    return page


def _ddg_amazon_product_urls(title: str, barcode: str = "",
                              max_urls: int = 3) -> list:
    """Busca fichas /dp/{ASIN} en amazon.es vía DDG. Cascada: título → EAN."""
    DDGS = _ddgs()
    if DDGS is None:
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
                    log.warning(f"  [amazon DDG] error ({attempt+1}/3): {e} "
                                f"— reintento {wait}s")
                    time.sleep(wait)
                else:
                    log.warning(f"  [amazon DDG] error final: {e}")

    log.info(f"  [amazon] {len(urls)} fichas: {urls}")
    return urls


def _extract_amazon_images(page) -> list:
    """
    Extrae URLs de la galería completa de un producto Amazon.

    1. colorImages JSON embebido en <script> → clave 'hiRes' (~2000px).
    2. DOM fallback — data-a-dynamic-image tomando la URL de mayor resolución.
    3. data-old-hires y src como último recurso.
    """
    seen_ids: set = set()
    out: list = []

    def _add(url: str):
        if not url or not _is_amazon_product_image(url) or _is_ui_sprite(url):
            return
        orig = strip_size_token(url)
        iid = _image_id(orig)
        if iid in seen_ids:
            return
        seen_ids.add(iid)
        out.append(orig)

    try:
        color_images = page.evaluate(r"""() => {
            for (const s of document.querySelectorAll('script')) {
                const m = s.textContent.match(/"colorImages"\s*:\s*\{"initial"\s*:\s*(\[[\s\S]*?\])\s*\}/);
                if (m) {
                    try { return JSON.parse(m[1]); } catch(e) {}
                }
            }
            return null;
        }""")
        if color_images:
            for item in (color_images or []):
                if not isinstance(item, dict):
                    continue
                for key in ('hiRes', 'large', 'mainUrl'):
                    url = item.get(key) or ''
                    if url:
                        _add(url)
                        break
            log.info(f"  [amazon] {len(out)} imgs vía colorImages")
    except Exception as e:
        log.debug(f"  [amazon] colorImages error: {e}")

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
                    try {
                        const parsed = JSON.parse(dyn);
                        const best = Object.entries(parsed)
                            .sort((a, b) => b[1][0] * b[1][1] - a[1][0] * a[1][1])[0];
                        if (best) urls.add(best[0]);
                    } catch(e) {}
                }
                const hires = img.getAttribute('data-old-hires');
                if (hires) urls.add(hires);
                if (img.src && !img.src.startsWith('data:')) urls.add(img.src);
            });
            return Array.from(urls);
        }""")
        for u in (raw or []):
            _add(u)
    except Exception as e:
        log.debug(f"  [amazon] DOM error: {e}")

    return out


def _search_via_playwright(title: str, barcode: str = "",
                            max_products: int = 3) -> list:
    """Fallback: navega la página de producto y extrae la galería completa.
    Solo fiable desde IP residencial (uso local)."""
    page = _get_page()
    if page is None:
        return []

    product_urls = _ddg_amazon_product_urls(title, barcode=barcode,
                                             max_urls=max_products)
    if not product_urls:
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

            try:
                is_captcha = page.query_selector(
                    '#captchacharacters, form[action*="validateCaptcha"]')
                if is_captcha:
                    log.info(f"  [amazon] CAPTCHA en {purl} — saltando")
                    time.sleep(3)
                    continue
            except Exception:
                pass

            asin_title = ""
            try:
                title_el = (
                    page.query_selector("span#productTitle") or
                    page.query_selector("#productTitle") or
                    page.query_selector("#title span") or
                    page.query_selector("h1.a-size-large")
                )
                if title_el:
                    asin_title = title_el.inner_text().strip()
            except Exception:
                pass

            if asin_title:
                sim = _title_sim(title, asin_title)
                log.info(f"  [amazon] '{asin_title[:70]}' sim={sim:.2f}")
                if sim < AMAZON_MATCH_THRESHOLD:
                    log.info(f"  [amazon] ASIN descartado — título no coincide")
                    continue

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

    log.info(f"  [amazon] total {len(all_imgs)} imágenes candidatas (Playwright)")
    return all_imgs


# ─── Interfaz pública ─────────────────────────────────────────────────────────

def search_amazon_image_urls(title: str, barcode: str = "",
                              max_products: int = 3) -> list:
    """
    Devuelve URLs de imágenes de producto Amazon en máxima resolución.

    Método principal: DDG image search (sin CAPTCHA, funciona en cualquier IP).
    Fallback opt-in (AMAZON_USE_PLAYWRIGHT=1): navega la página de producto
    con Playwright — solo recomendado en local (IP residencial).
    """
    urls = _search_via_ddg_images(title, barcode=barcode)
    if urls:
        return urls

    if os.getenv("AMAZON_USE_PLAYWRIGHT", "").lower() in ("1", "true", "yes"):
        log.info("  [amazon] DDG image search sin resultados — fallback Playwright")
        return _search_via_playwright(title, barcode=barcode,
                                      max_products=max_products)
    return []
