"""
Scraper para Beaphar — beaphar.es

Las páginas de producto usan una URL con ID numérico interno que NO se puede
derivar del título Shopify:

    https://www.beaphar.es/product/{id}-{slug}/
    p.ej. https://www.beaphar.es/product/19973-champu-perros-universal-1l/

Por eso, a diferencia de Artero, aquí no se puede construir el slug directo.
La resolución es siempre bajo demanda vía DuckDuckGo con filtro
`site:beaphar.es/product/`, con sanity check del h1 contra el título Shopify.

Además el sitio bloquea peticiones sin navegador real (HTTP 403), por lo que
toda la navegación se hace con Playwright + user-agent de Chrome.

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

CATALOG_PATH = Path("resultados/beaphar_catalog.json")
PRODUCT_PATH = "beaphar.es/product/"
MIN_SCORE    = 0.10

# Marcadores internos que añade la tienda a los títulos:
#   *DX*  → producto descatalogado/outlet
#   (NDR), (PV)... → notas internas
_TITLE_NOISE = re.compile(r'\s*(?:\*[^*]*\*|\((?:NDR|PV|NV|ONLINE)\))\s*', re.IGNORECASE)

IGNORE_TOKENS = {
    "beaphar", "beapharm",
    "de", "el", "la", "los", "las", "con", "sin", "y", "e", "o", "a", "para", "un", "una",
    "ml", "gr", "g", "kg", "l", "cm", "mm", "x", "ud", "uds", "comp", "cpd",
    "dx",
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


def _title_key(title: str) -> str:
    """Clave de caché estable derivada del título: 'champu perros universal' → slug."""
    norm = _normalize(_clean_title(title))
    return re.sub(r"\s+", "-", norm.strip()).strip("-")


def _tokenize(text: str) -> set:
    tokens = set(_normalize(text).split())
    tokens -= IGNORE_TOKENS
    tokens -= {t for t in tokens if len(t) <= 1}
    return tokens


def _stem(token: str) -> str:
    """Stemming mínimo para plurales españoles: 'gatos'→'gato', 'perros'→'perro'.
    Permite que 'gato' (título Shopify) case con 'gatos' (web)."""
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _stem_set(tokens: set) -> set:
    return {_stem(t) for t in tokens}


def _similarity(title_tokens: set, name_tokens: set) -> float:
    """Jaccard sobre tokens con stemming. Penaliza tanto tokens del título que
    faltan como tokens extra en el nombre web (evita que 'champu repelente perro
    gato' gane a 'champu gatos' al matchear 'champu gato')."""
    a = _stem_set(title_tokens)
    b = _stem_set(name_tokens)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# Umbral mínimo de similitud para aceptar un candidato DDG
MATCH_THRESHOLD = 0.34


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


# ─── Filtrado de URLs de imagen ───────────────────────────────────────────────

def _should_keep_url(url: str) -> bool:
    """Descarta URLs no-producto (logos, iconos, banners, placeholders)."""
    low = url.lower()
    if any(kw in low for kw in (".svg", "logo", "icon", "banner", "sprite",
                                 "placeholder", "favicon", "/static/",
                                 "loader", "spinner", "flag", "pixel",
                                 "hqdefault", "maxresdefault", "sddefault")):
        return False
    return True


def _strip_size_suffix(url: str) -> str:
    """WordPress añade '-300x300' antes de la extensión para los thumbnails.
    Lo quitamos para apuntar a la imagen original a tamaño completo."""
    return re.sub(r'-\d+x\d+(\.[a-zA-Z]{3,4})(?:\?.*)?$', r'\1', url.split("?")[0])


def _filter_by_ean(images: list) -> list:
    """Beaphar's CDN names files after the EAN: .../8711231199877.jpg,
    .../8711231199877_1.jpg, etc. Related-product images from the same CDN have
    different EANs. Extract the EAN from the first (og:image) URL and discard
    CDN images whose filename doesn't start with that EAN."""
    if not images:
        return images
    ean_match = re.search(r'/(\d{13})(?:_\d+)?\.[a-zA-Z]', images[0])
    if not ean_match:
        return images
    ean = ean_match.group(1)
    cdn_host = urlparse(images[0]).netloc
    filtered = []
    for url in images:
        if urlparse(url).netloc == cdn_host:
            if re.search(rf'/{ean}(?:_\d+)?\.[a-zA-Z]', url):
                filtered.append(url)
        else:
            filtered.append(url)
    log.info(f"    EAN filter ({ean}): {len(images)} → {len(filtered)} imgs")
    return filtered


# ─── Extracción de imágenes ───────────────────────────────────────────────────

def _extract_images(page, page_url: str) -> list:
    """
    Extrae URLs de imágenes en orden de aparición: og:image primero, luego
    JSON-LD, luego <img> del DOM. Descarta thumbnails reduciendo el sufijo
    -WxH de WordPress.

    No filtra por host: beaphar.es usa CloudFront (d7rh5s3nxmpy4.cloudfront.net)
    tanto para og:image como para la galería, y el dominio no contiene "beaphar".
    El filtro de junk lo hace _should_keep_url (logos, icons, banners, pixels...).
    """
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

    # 1. og:image — imagen principal canónica del producto
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

    # 3. <img> en orden DOM — solo galería del producto, excluye relacionados/upsells
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

    result = _filter_by_ean(ordered)
    log.info(f"    Imágenes extraídas: {len(result)}")
    return result


def _try_url(page, url: str) -> tuple:
    """
    Visita la URL y devuelve (name, images) de la página de producto.
    No decide el match aquí: la puntuación/ranking se hace en find_best_match().
    Devuelve (None, []) solo si la página no es válida (sin h1, error HTTP).
    """
    try:
        resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
        if resp and resp.status >= 400:
            log.debug(f"  HTTP {resp.status} en {url}")
            return None, []

        # Esperar network idle para que WordPress/WooCommerce cargue imágenes lazy
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
            return None, []

        images = _extract_images(page, page.url)
        return name, images
    except Exception as e:
        log.debug(f"  _try_url error: {e}")
        return None, []


# ─── Búsqueda DDG con filtro site: ────────────────────────────────────────────

def get_ddg_query(title: str) -> str:
    """Query limpia para DDG (usada también por el fallback web_y_amazon)."""
    return f"beaphar {_clean_title(title)} product image"


def _ddg_query_urls(query: str, max_urls: int, seen: set) -> list:
    """Ejecuta una query DDG y devuelve URLs de producto nuevas (no en seen)."""
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
    """Devuelve hasta `max_urls` URLs candidatas de beaphar.es/product/.

    Estrategia en cascada:
      1. Query con título completo (incluye 'BEAPHAR')
      2. Si faltan candidatos: query sin prefijo 'BEAPHAR/BEAPHARM'
      3. Si barcode dado y aún faltan: query por EAN
    """
    seen: set = set()
    urls: list = []

    clean = _clean_title(title)

    # 1. Query con título completo
    q1 = f"site:beaphar.es/product/ {clean}"
    log.info(f"  [DDG] {q1}")
    urls += _ddg_query_urls(q1, max_urls - len(urls), seen)

    # 2. Sin prefijo de marca si quedan huecos
    if len(urls) < max_urls:
        no_brand = re.sub(r'^(?:BEAPHAR|BEAPHARM)\s+', '', clean,
                          flags=re.IGNORECASE).strip()
        if no_brand != clean:
            q2 = f"site:beaphar.es/product/ {no_brand}"
            log.info(f"  [DDG fallback sin marca] {q2}")
            urls += _ddg_query_urls(q2, max_urls - len(urls), seen)

    # 3. Fallback por EAN si se proporcionó y aún faltan candidatos
    if len(urls) < 2 and barcode:
        q3 = f"site:beaphar.es/product/ {barcode}"
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
    Carga el catálogo cacheado. No hace scraping bulk: beaphar.es no expone un
    listado fiable y las URLs requieren un ID interno, así que cada producto se
    resuelve bajo demanda en find_best_match() vía DDG.
    Con rebuild=True se borra el caché.
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
    Resolución por producto:
      1. Cache hit por clave de título normalizada
      2. DDG con site:beaphar.es/product/ → recolecta varios candidatos
         (con fallback sin prefijo marca y por EAN si barcode dado),
         puntúa cada h1 con _similarity() y elige el de mayor score
      3. Cachear bajo la clave de título y el slug de la URL
    Devuelve (handle, score) donde handle es una clave de `catalog`.
    """
    title_tokens = _tokenize(shopify_title)
    title_key = _title_key(shopify_title)

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
        name, images = _try_url(page, url)
        if not name:
            continue
        score = _similarity(title_tokens, _tokenize(name))
        log.info(f"  [cand] score={score:.2f} '{name}' ({len(images)} imgs) {url}")
        if best is None or score > best[0]:
            best = (score, name, images, url)
        if score >= 0.85:   # match casi perfecto: no seguir probando
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
