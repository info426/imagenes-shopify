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


# ─── Extracción de imágenes ───────────────────────────────────────────────────

def _extract_images(page, page_url: str) -> list:
    """
    Extrae URLs de imágenes en orden de aparición: og:image primero, luego
    JSON-LD, luego <img> del DOM. Solo conserva imágenes del propio dominio
    beaphar (resolviendo URLs relativas) y descarta thumbnails reduciendo
    el sufijo -WxH de WordPress.
    """
    host = urlparse(page_url).netloc.lower()
    # Dominio base sin subdominio (ej. "beaphar.es") para aceptar CDN propio
    base_domain = ".".join(host.split(".")[-2:]) if host.count(".") >= 1 else host
    ordered: list = []
    seen: set = set()

    def _add(raw_url: str):
        if not raw_url or raw_url.startswith("data:"):
            return
        full = urljoin(page_url, raw_url)
        if not full.startswith("http"):
            return
        netloc = urlparse(full).netloc.lower()
        # Aceptar mismo host, subdominios del sitio y CDNs que contengan "beaphar"
        if netloc != host and base_domain not in netloc and "beaphar" not in netloc:
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
                log.debug(f"    og:image raw: {val}")
                _add(val)
    except Exception as e:
        log.debug(f"    og:image error: {e}")

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

    # 3. <img> en orden DOM (galería del producto)
    try:
        imgs_found = page.query_selector_all("img")
        log.debug(f"    DOM imgs total: {len(imgs_found)}")
        for el in imgs_found:
            srcset = el.get_attribute("srcset") or ""
            parts  = [p.strip() for p in srcset.split(",") if p.strip()]
            if parts:
                cand = parts[-1].split()[0]   # mayor resolución del srcset
                _add(cand)
            for attr in ("data-src", "data-lazy-src", "data-large_image",
                         "data-zoom-image", "data-full-url", "src"):
                _add(el.get_attribute(attr) or "")
    except Exception as e:
        log.debug(f"    DOM imgs error: {e}")

    log.debug(f"    Imágenes extraídas: {len(ordered)}")
    return ordered


def _try_url(page, url: str, title_tokens: set) -> tuple:
    """
    Visita la URL y devuelve (name, images) si es una página de producto válida
    cuyo h1 comparte tokens con el título Shopify. Si no, (None, []).
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

        name_tokens = _tokenize(name)
        if title_tokens:
            overlap = title_tokens & name_tokens
            # Exigimos que la proporción de overlap sea alta para evitar falsos
            # positivos entre productos similares (ej. champu gato vs champu repelente).
            # Jaccard mínimo 0.5: la mitad de los tokens del título deben coincidir.
            jaccard = len(overlap) / len(title_tokens | name_tokens) if (title_tokens | name_tokens) else 0
            min_overlap = max(2, len(title_tokens) - 1) if len(title_tokens) >= 3 else 1
            if len(overlap) < min_overlap:
                log.info(f"  Sanity check falla ({len(overlap)}/{min_overlap} tokens, "
                         f"jaccard={jaccard:.2f}) "
                         f"{sorted(title_tokens)} ∩ {sorted(name_tokens)}: '{name}'")
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
    query = f"site:beaphar.es/product/ {clean}"
    log.info(f"  [DDG] {query}")
    for attempt in range(3):
        try:
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=10):
                    url = r.get("href") or r.get("url") or ""
                    if PRODUCT_PATH not in url:
                        continue
                    url = url.split("?")[0].split("#")[0].rstrip("/") + "/"
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


def find_best_match(shopify_title: str, catalog: dict) -> tuple:
    """
    Resolución por producto:
      1. Cache hit por clave de título normalizada
      2. DDG con site:beaphar.es/product/ + sanity check del h1
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

    # 2. DDG
    url = _ddg_find_product_url(shopify_title)
    if url:
        name, images = _try_url(page, url, title_tokens)
        if name:
            entry = {"name": name, "url": url, "images": images}
            catalog[title_key] = entry
            slug = url.rstrip("/").split("/")[-1]
            if slug and slug != title_key:
                catalog[slug] = entry
            _save_catalog(catalog)
            log.info(f"  ✓ Resuelto vía DDG: {name} ({len(images)} imgs)")
            return title_key, 1.0

    log.warning(f"  Sin resolución para '{shopify_title}'")
    return None, 0.0
