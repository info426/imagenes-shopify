"""
Scraper para Menforsan — menforsan.com

El sitio bloquea peticiones sin navegador real (HTTP 403), por lo que
toda la navegación se hace con Playwright + user-agent de Chrome.

Las URLs de producto siguen el patrón típico de WooCommerce español:
  https://www.menforsan.com/producto/{slug}/
El slug puede derivarse del título en algunos casos, pero dado que el
sitio devuelve 403 sin navegador no se puede confirmar sin el runner.
Por eso se usa resolución bajo demanda vía DuckDuckGo con filtro
`site:menforsan.com`, con sanity check del h1 contra el título Shopify.

Interfaz estándar (requerida por core/process_brand.py):
  scrape_catalog(web_url, rebuild=False) -> dict
  find_best_match(shopify_title, catalog) -> (handle, score)
  get_ddg_query(shopify_title) -> str   (opcional, para fallback web_y_amazon)
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

CATALOG_PATH  = Path("resultados/menforsan_catalog.json")
PRODUCT_PATH  = "menforsan.com"   # se restringe a URLs de producto en _is_product_url
MIN_SCORE     = 0.10

# Marcadores internos de la tienda que no ayudan al matching
_TITLE_NOISE = re.compile(
    r'\s*(?:\*[^*]*\*|\((?:NDR|PV|NV|ONLINE)\))\s*', re.IGNORECASE
)

IGNORE_TOKENS = {
    "menforsan",
    "de", "el", "la", "los", "las", "con", "sin", "y", "e", "o", "a", "para",
    "un", "una", "al",
    "ml", "gr", "g", "kg", "l", "cm", "mm", "x", "ud", "uds",
    "dx",
}

# Rutas que NO son páginas de producto individual
_NON_PRODUCT_PATHS = re.compile(
    r"/(categoria|category|tag|etiqueta|tienda|shop|cart|checkout|"
    r"mi-cuenta|my-account|blog|noticias|contacto|contact|"
    r"sobre-nosotros|quienes-somos)/",
    re.IGNORECASE
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Threshold mínimo de similitud para aceptar un candidato DDG
MATCH_THRESHOLD = 0.30

# Estado Playwright reutilizado entre llamadas a find_best_match()
_PW = {"pw": None, "browser": None, "ctx": None, "page": None}


# ─── Utilidades de texto ──────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9\s]", " ", text)


def _clean_title(title: str) -> str:
    return _TITLE_NOISE.sub(" ", title).strip()


def _title_key(title: str) -> str:
    """Clave de caché estable: 'MENFORSAN CHAMPÚ PERROS 400 ML' → slug."""
    norm = _normalize(_clean_title(title))
    return re.sub(r"\s+", "-", norm.strip()).strip("-")


def _tokenize(text: str) -> set:
    tokens = set(_normalize(text).split())
    tokens -= IGNORE_TOKENS
    tokens -= {t for t in tokens if len(t) <= 1}
    return tokens


def _stem(token: str) -> str:
    """Stemming mínimo para plurales españoles: 'gatos'→'gato', 'perros'→'perro'."""
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _stem_set(tokens: set) -> set:
    return {_stem(t) for t in tokens}


def _similarity(title_tokens: set, name_tokens: set) -> float:
    """Jaccard con stemming."""
    a = _stem_set(title_tokens)
    b = _stem_set(name_tokens)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ─── Playwright lazy init ─────────────────────────────────────────────────────

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
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
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
    _PW.update({"pw": pw, "browser": browser, "ctx": ctx, "page": page})

    def _cleanup():
        try:
            browser.close()
            pw.stop()
        except Exception:
            pass
    atexit.register(_cleanup)

    # Warm-up en homepage para establecer cookies y parecer navegador real
    try:
        page.goto("https://www.menforsan.com/", timeout=20000,
                  wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        log.info("Warm-up menforsan.com OK")
    except Exception as e:
        log.debug(f"Warm-up failed: {e}")

    return page


# ─── Filtrado de URLs de imagen ───────────────────────────────────────────────

def _should_keep_url(url: str) -> bool:
    low = url.lower()
    if any(kw in low for kw in (".svg", "logo", "icon", "banner", "sprite",
                                 "placeholder", "favicon", "/static/",
                                 "loader", "spinner", "flag", "pixel",
                                 "hqdefault", "maxresdefault", "sddefault")):
        return False
    return True


def _strip_size_suffix(url: str) -> str:
    """Quita sufijo WordPress -WxH para obtener imagen original (clave de deduplicación)."""
    url = url.split("?")[0]
    return re.sub(r'-\d+x\d+(\.[a-zA-Z]{3,4})$', r'\1', url)


# Tamaños PrestaShop pequeños que se deben subir a large_default
_PS_UPGRADE = re.compile(
    r'/(\d+)-(?:cart|small|medium|home|category)_default/'
)

def _upgrade_prestashop_url(url: str) -> str:
    """Sube variantes pequeñas PrestaShop a large_default (~800-1000px)."""
    return _PS_UPGRADE.sub(r'/\1-large_default/', url)


def _filter_by_ean(images: list, barcode: str = "") -> list:
    """Si el CDN nombra los ficheros con el EAN, descarta imágenes de otros EANs.
    Si no hay patrón EAN en la primera URL, devuelve la lista sin modificar."""
    if not images:
        return images
    # Intentar extraer EAN de la primera imagen (og:image suele ser la principal)
    ean_match = re.search(r'/(\d{13})(?:_\d+)?\.[a-zA-Z]', images[0])
    if not ean_match:
        # Fallback: usar barcode si se proporcionó y aparece en alguna URL
        if barcode and len(barcode) >= 8:
            for img in images:
                if barcode in img:
                    ean_match = re.search(rf'/({re.escape(barcode)})(?:_\d+)?\.[a-zA-Z]', img)
                    if ean_match:
                        break
    if not ean_match:
        return images
    ean = ean_match.group(1)
    cdn_host = urlparse(images[0]).netloc
    filtered = []
    for url in images:
        if urlparse(url).netloc == cdn_host:
            if re.search(rf'/{re.escape(ean)}(?:_\d+)?\.[a-zA-Z]', url):
                filtered.append(url)
        else:
            filtered.append(url)
    log.info(f"    EAN filter ({ean}): {len(images)} → {len(filtered)} imgs")
    return filtered


# ─── Extracción de imágenes ───────────────────────────────────────────────────

def _extract_images(page, page_url: str, barcode: str = "") -> list:
    """og:image → JSON-LD → <img> DOM (excluye relacionados/upsells)."""
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
        # Subir miniaturas PrestaShop (medium/small/...) a large_default
        full = _upgrade_prestashop_url(full)
        clean = _strip_size_suffix(full)
        if clean in seen:
            return
        seen.add(clean)
        ordered.append(full)  # URL real (large_default), no la clave de dedup

    # 1. og:image
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

    # 3. DOM — excluye relacionados/upsells/footer y thumbnails pequeños (PrestaShop)
    try:
        img_data = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('img'))
                .filter(img => {
                    if (img.closest('.related') || img.closest('.upsells') ||
                        img.closest('.cross-sells') ||
                        img.closest('[class*="related"]') ||
                        img.closest('[class*="upsell"]') ||
                        img.closest('[id*="related"]') ||
                        img.closest('footer') || img.closest('header') ||
                        img.closest('nav')) return false;
                    // PrestaShop: descartar miniaturas de productos del carrusel
                    if (img.closest('.product-miniature') ||
                        img.closest('.js-product-miniature') ||
                        img.closest('[class*="miniature"]') ||
                        img.closest('[class*="product-list"]') ||
                        img.closest('[class*="products-grid"]')) return false;
                    // Descartar por tamaño renderizado (thumbnails < 200px)
                    if (img.naturalWidth > 0 && img.naturalWidth < 200) return false;
                    return true;
                })
                .map(img => ({
                    srcset:           img.getAttribute('srcset')           || '',
                    dataSrc:          img.getAttribute('data-src')         || '',
                    dataLazySrc:      img.getAttribute('data-lazy-src')    || '',
                    dataLargeImage:   img.getAttribute('data-large_image') || '',
                    dataZoomImage:    img.getAttribute('data-zoom-image')  || '',
                    dataFullUrl:      img.getAttribute('data-full-url')    || '',
                    src:              img.getAttribute('src')              || ''
                }));
        }""")
        log.info(f"    DOM imgs (excl. relacionados): {len(img_data or [])}")
        for item in (img_data or []):
            srcset = item.get("srcset", "")
            parts  = [p.strip() for p in srcset.split(",") if p.strip()]
            if parts:
                _add(parts[-1].split()[0])
            for key in ("dataSrc", "dataLazySrc", "dataLargeImage",
                        "dataZoomImage", "dataFullUrl", "src"):
                _add(item.get(key, ""))
    except Exception as e:
        log.info(f"    DOM imgs error: {e}")

    result = _filter_by_ean(ordered, barcode=barcode)
    log.info(f"    Imágenes extraídas: {len(result)}")
    return result


def _try_url(page, url: str, barcode: str = "") -> tuple:
    """Visita la URL y devuelve (name, images). (None, []) si la página es inválida."""
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
            log.info(f"  h1 vacío/no encontrado en {url}")
            return None, []

        images = _extract_images(page, page.url, barcode=barcode)
        return name, images
    except Exception as e:
        log.debug(f"  _try_url error: {e}")
        return None, []


# ─── URL helpers ──────────────────────────────────────────────────────────────

def _is_product_url(url: str) -> bool:
    """Descarta URLs de categorías, blog, etc.
    menforsan.com usa PrestaShop: /es/{categoria}/{id}-{slug}.html
    Solo acepta sección española (/es/) para evitar falsos positivos con /en/."""
    parsed = urlparse(url)
    if PRODUCT_PATH not in url:
        return False
    # Exigir sección española — la sección /en/ da scores bajos (títulos en inglés)
    # y puede no coincidir bien con el título Shopify en español.
    if "/es/" not in parsed.path:
        return False
    if _NON_PRODUCT_PATHS.search(parsed.path):
        return False
    # Solo aceptar páginas de producto PrestaShop: último segmento = {id}-{slug}.html
    last = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return bool(re.match(r'^\d+-.+\.html$', last, re.IGNORECASE))


# ─── Búsqueda DDG ─────────────────────────────────────────────────────────────

def get_ddg_query(title: str) -> str:
    """Query limpia para DDG (usada también por el fallback web_y_amazon)."""
    return f"menforsan {_clean_title(title)} producto"


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
                    url = url.split("?")[0].split("#")[0].rstrip("/")
                    if not url.lower().endswith(".html"):
                        url += "/"
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
    """Cascada: 1) título completo, 2) sin prefijo MENFORSAN, 3) por EAN."""
    seen: set = set()
    urls: list = []
    clean = _clean_title(title)

    # 1. Título completo — restringir a /es/ para evitar URLs en inglés (/en/)
    #    que dan scores bajos y pueden causar falsos positivos con otros productos.
    q1 = f"site:menforsan.com/es/ {clean}"
    log.info(f"  [DDG] {q1}")
    urls += _ddg_query_urls(q1, max_urls - len(urls), seen)

    # 2. Sin prefijo de marca
    if len(urls) < max_urls:
        no_brand = re.sub(r'^MENFORSAN\s+', '', clean, flags=re.IGNORECASE).strip()
        if no_brand != clean:
            q2 = f"site:menforsan.com/es/ {no_brand}"
            log.info(f"  [DDG fallback sin marca] {q2}")
            urls += _ddg_query_urls(q2, max_urls - len(urls), seen)

    # 3. Fallback por EAN
    if len(urls) < 2 and barcode:
        q3 = f"site:menforsan.com/es/ {barcode}"
        log.info(f"  [DDG fallback EAN] {q3}")
        urls += _ddg_query_urls(q3, max_urls - len(urls), seen)

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
        log.warning(f"No se pudo guardar catálogo: {e}")


# ─── Interfaz pública ─────────────────────────────────────────────────────────

def scrape_catalog(web_url: str, rebuild: bool = False) -> dict:
    """
    Carga el catálogo cacheado. La resolución real es bajo demanda en
    find_best_match() vía DDG (menforsan.com no expone un listado estático fiable).
    Con rebuild=True borra el caché.
    """
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


def find_best_match(shopify_title: str, catalog: dict,
                    barcode: str = "") -> tuple:
    """
    1. Cache hit por clave de título normalizada.
    2. DDG con site:menforsan.com → recolecta candidatos,
       puntúa h1 con Jaccard + stemming ES, elige el de mayor score.
    3. Cachea el resultado.
    Devuelve (handle, score).
    """
    title_tokens = _tokenize(shopify_title)
    title_key    = _title_key(shopify_title)

    # 1. Cache exacto
    if title_key in catalog:
        log.info(f"  Match caché: {title_key}")
        return title_key, 1.0

    page = _get_page()
    if page is None:
        return None, 0.0

    # 2. DDG: recolectar candidatos y elegir el de mayor similitud
    urls = _ddg_find_product_urls(shopify_title, barcode=barcode)
    best = None   # (score, name, images, url)
    for url in urls:
        name, images = _try_url(page, url, barcode=barcode)
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
        slug = url.rstrip("/").split("/")[-1]
        if slug and slug != title_key:
            catalog[slug] = entry
        _save_catalog(catalog)
        log.info(f"  ✓ Resuelto vía DDG: {name} (score={score:.2f}, {len(images)} imgs)")
        return title_key, score

    if best:
        log.warning(f"  Mejor candidato score={best[0]:.2f} < {MATCH_THRESHOLD} "
                    f"— descartado: '{best[1]}'")
    log.warning(f"  Sin resolución para '{shopify_title}'")
    return None, 0.0
