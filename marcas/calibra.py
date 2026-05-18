"""
Scraper para Calibra — mycalibra.es / mycalibra.eu / calibra.cat.
Usa Playwright porque los tres dominios devuelven 403 a requests directos.

Interfaz estándar (requerida por core/process_brand.py):
  scrape_catalog(web_url, rebuild=False) -> dict
  find_best_match(shopify_title, catalog) -> (handle, score)
"""

import json
import logging
import re
import time
import unicodedata
from pathlib import Path

log = logging.getLogger(__name__)

CATALOG_PATH = Path("resultados/calibra_catalog.json")

# Fuentes a scrapear, en orden de prioridad.
# mycalibra.es primero porque los títulos en español coinciden mejor con Shopify.
SOURCES = [
    {
        "base":  "https://www.mycalibra.es",
        "lang":  "es",
        "categories": [
            "https://www.mycalibra.es/comida-para-perros",
            "https://www.mycalibra.es/comida-para-gatos",
            "https://www.mycalibra.es/productos",
        ],
    },
    {
        "base":  "https://www.mycalibra.eu",
        "lang":  "eu",
        "categories": [
            "https://www.mycalibra.eu/food-for-dogs",
            "https://www.mycalibra.eu/food-for-cats",
            "https://www.mycalibra.eu/products",
        ],
    },
    {
        "base":  "https://calibra.cat",
        "lang":  "cat",
        "categories": [
            "https://calibra.cat/food-for-dogs",
            "https://calibra.cat/food-for-cats",
            "https://calibra.cat/products",
        ],
    },
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

IGNORE_TOKENS = {
    "calibra",
    "de", "el", "la", "los", "las", "con", "sin", "y", "para",
    "kg", "gr", "g", "lb", "x", "ml", "new",
}

MIN_SCORE = 0.12

# Palabras en URL que indican página de categoría o de contenido (no producto)
_NON_PRODUCT_PATHS = {
    "comida-para-perros", "comida-para-gatos", "productos", "food-for-dogs",
    "food-for-cats", "products", "product-lines", "dry-food", "wet-food",
    "treats", "dental", "veterinary", "dietas-veterinarias", "our-story",
    "junior", "senior", "news", "blog", "contact", "donde-comprar",
    "where-to-buy", "legal", "privacy", "cookies",
}

# Número máximo de páginas de paginación a intentar por categoría
_MAX_PAGES = 25

# Parámetro de paginación detectado en mycalibra.es
_PAGER_PARAM = "pager-page"


# ─── Extracción de imágenes ───────────────────────────────────────────────────

def _bump_resolution(url: str) -> str:
    """Elimina parámetros de ancho pequeño y solicita máxima resolución."""
    url_clean = re.sub(r'_\d+x\d*\.', '.', url)          # quitar _NNNx
    url_clean = re.sub(r'\?.*$', '', url_clean)            # quitar querystring
    return url_clean


def _extract_images(page, base_url: str) -> list:
    """Extrae URLs de imágenes del producto en la página actual."""
    urls: set = set()

    # 1. <img src> con pistas de producto
    try:
        for el in page.query_selector_all("img[src]"):
            src = el.get_attribute("src") or ""
            if not src or src.startswith("data:"):
                continue
            try:
                w = page.evaluate("el => el.naturalWidth", el) or 0
                if 0 < w < 200:
                    continue
            except Exception:
                pass
            kw = src.lower()
            if any(k in kw for k in ("/product", "/products", "/catalog",
                                      "product-image", "/media/", "wp-content",
                                      "/files/", "/images/calibra", "/img/")):
                full = _abs(src, base_url)
                if full:
                    urls.add(_bump_resolution(full))
    except Exception:
        pass

    # 2. srcset — quedarse con la versión más grande (último elemento)
    try:
        for el in page.query_selector_all("img[srcset]"):
            srcset = el.get_attribute("srcset") or ""
            parts = [p.strip() for p in srcset.split(",") if p.strip()]
            if parts:
                candidate = parts[-1].split()[0]
                if candidate and not candidate.startswith("data:"):
                    full = _abs(candidate, base_url)
                    if full:
                        urls.add(_bump_resolution(full))
    except Exception:
        pass

    # 3. lazy-loading attrs
    for attr in ("data-src", "data-lazy-src", "data-original", "data-zoom-image",
                 "data-full-url", "data-large"):
        try:
            for el in page.query_selector_all(f"img[{attr}]"):
                src = el.get_attribute(attr) or ""
                if src and not src.startswith("data:"):
                    full = _abs(src, base_url)
                    if full:
                        urls.add(_bump_resolution(full))
        except Exception:
            pass

    # 4. JSON-LD schema.org Product
    try:
        ld_texts = page.evaluate("""() =>
            Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
                 .map(s => s.textContent || '')
        """)
        for text in (ld_texts or []):
            try:
                data = json.loads(text)
                _collect_ld_images(data, base_url, urls)
            except Exception:
                pass
    except Exception:
        pass

    # 5. Variables JS (WooCommerce / custom galleries)
    try:
        js_texts = page.evaluate("""() =>
            Array.from(document.querySelectorAll('script:not([src])'))
                 .map(s => s.textContent || '')
                 .filter(t => t.includes('gallery') || t.includes('product_image')
                            || t.includes('"image"') || t.includes("'image'"))
        """)
        for text in (js_texts or []):
            for m in re.finditer(
                r'https?://[^\s"\'<>\\]+\.(?:jpg|jpeg|png|webp)[^\s"\'<>\\]*', text
            ):
                u = m.group(0).rstrip("\\,;")
                if len(u) < 500 and any(
                    base_url.split("//")[1].split("/")[0] in u
                    for base_url in [s["base"] for s in SOURCES]
                ):
                    urls.add(_bump_resolution(u))
    except Exception:
        pass

    return list(urls)


def _collect_ld_images(data, base_url: str, urls: set):
    """Extrae imágenes de un objeto JSON-LD recursivamente."""
    if isinstance(data, list):
        for item in data:
            _collect_ld_images(item, base_url, urls)
        return
    if not isinstance(data, dict):
        return
    for key in ("image", "thumbnail", "logo"):
        val = data.get(key)
        if isinstance(val, str) and val.startswith("http"):
            urls.add(_bump_resolution(val))
        elif isinstance(val, dict):
            u = val.get("url") or val.get("contentUrl", "")
            if u:
                urls.add(_bump_resolution(u))
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, str) and item.startswith("http"):
                    urls.add(_bump_resolution(item))
                elif isinstance(item, dict):
                    u = item.get("url") or item.get("contentUrl", "")
                    if u:
                        urls.add(_bump_resolution(u))


def _abs(url: str, base: str) -> str:
    """Convierte URL relativa a absoluta."""
    if url.startswith("http"):
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return base.rstrip("/") + url
    return ""


# ─── Recolección de URLs de producto ─────────────────────────────────────────

def _is_product_url(href: str, base: str) -> bool:
    """Devuelve True si el href parece una página de producto individual."""
    if not href.startswith(base):
        return False
    path = href.rstrip("/").split("?")[0][len(base):]
    segments = [s for s in path.split("/") if s]
    if len(segments) != 1:
        return False
    slug = segments[0]
    if slug in _NON_PRODUCT_PATHS:
        return False
    if not slug.startswith("calibra"):
        return False
    return True


def _collect_product_links(page, base: str) -> set:
    """Recolecta hrefs que parecen páginas de producto individual."""
    found: set = set()
    try:
        for el in page.query_selector_all("a[href]"):
            href = (el.get_attribute("href") or "").split("?")[0].rstrip("/")
            if href.startswith("/"):
                href = base + href
            if _is_product_url(href, base):
                found.add(href)
    except Exception as e:
        log.debug(f"  _collect_product_links error: {e}")
    return found


def _scrape_source(page, source: dict) -> dict:
    """
    Scrapea todas las categorías de una fuente, con paginación,
    y devuelve el catálogo parcial.
    """
    base       = source["base"]
    categories = source["categories"]
    catalog: dict = {}
    all_product_urls: set = set()

    for cat_url in categories:
        log.info(f"  Recogiendo links: {cat_url}")

        # Iterar páginas
        for page_num in range(1, _MAX_PAGES + 1):
            url = cat_url if page_num == 1 else f"{cat_url}?{_PAGER_PARAM}={page_num}"
            try:
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1500)
            except Exception as e:
                log.warning(f"    Error cargando {url}: {e}")
                break

            links_before = len(all_product_urls)
            new_links = _collect_product_links(page, base)
            all_product_urls |= new_links

            log.info(f"    Página {page_num}: {len(new_links)} nuevos links "
                     f"({len(all_product_urls)} total)")

            # Si la página no aportó links nuevos, hemos llegado al final
            if len(all_product_urls) == links_before and page_num > 1:
                break

    log.info(f"  {len(all_product_urls)} productos únicos a procesar de {base}")

    for prod_url in sorted(all_product_urls):
        slug = prod_url.rstrip("/").split("/")[-1]
        if slug in catalog:
            continue

        try:
            page.goto(prod_url, timeout=45000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(500)

            name_el = (
                page.query_selector("h1.product-name")
                or page.query_selector("h1.product-title")
                or page.query_selector(".product-name h1")
                or page.query_selector("h1")
            )
            name   = name_el.inner_text().strip() if name_el else slug
            images = _extract_images(page, base)

            catalog[slug] = {
                "name":   name,
                "url":    prod_url,
                "source": source["lang"],
                "images": images,
            }
            log.info(f"    {slug}: {len(images)} img — {name[:60]}")
            time.sleep(0.8)

        except Exception as e:
            log.warning(f"    Error en {prod_url}: {e}")

    return catalog


# ─── Interfaz pública: scrape_catalog ────────────────────────────────────────

def scrape_catalog(web_url: str = "", rebuild: bool = False) -> dict:
    """
    Scrapea mycalibra.es (primaria), mycalibra.eu y calibra.cat con Playwright.
    Devuelve: { slug: { name, url, source, images: [url, ...] } }
    Cachea en resultados/calibra_catalog.json.
    El parámetro web_url se ignora; las URLs están hardcodeadas por fuente.
    """
    if not rebuild and CATALOG_PATH.exists():
        try:
            catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
            log.info(f"Catálogo cargado desde caché: {len(catalog)} productos")
            return catalog
        except Exception:
            pass

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright no instalado: pip install playwright && "
                  "playwright install chromium")
        return {}

    catalog: dict = {}
    log.info("Scraping catálogo Calibra con Playwright...")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"],
        )
        ctx = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="es-ES",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]},
            ignore_https_errors=True,
        )
        page = ctx.new_page()

        for source in SOURCES:
            log.info(f"\n=== Fuente: {source['base']} ===")
            try:
                entries = _scrape_source(page, source)
                # Solo añadir productos no vistos en fuentes anteriores
                new_count = 0
                for slug, entry in entries.items():
                    if slug not in catalog:
                        catalog[slug] = entry
                        new_count += 1
                log.info(f"  → {new_count} productos nuevos de {source['base']}")
            except Exception as e:
                log.warning(f"  Error scraping {source['base']}: {e}")

        browser.close()

    CATALOG_PATH.parent.mkdir(exist_ok=True)
    CATALOG_PATH.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(f"\nCatálogo guardado: {len(catalog)} productos → {CATALOG_PATH}")
    return catalog


# ─── Matching ─────────────────────────────────────────────────────────────────

_SYNONYMS: dict[str, set] = {
    "pollo":        {"chicken"},
    "chicken":      {"pollo"},
    "cordero":      {"lamb"},
    "lamb":         {"cordero"},
    "arroz":        {"rice"},
    "rice":         {"arroz"},
    "salmon":       {"salmon"},
    "salmón":       {"salmon"},
    "ternera":      {"beef", "veal"},
    "buey":         {"ox", "beef"},
    "beef":         {"ternera", "buey"},
    "veal":         {"ternera"},
    "pavo":         {"turkey"},
    "turkey":       {"pavo"},
    "atun":         {"tuna"},
    "tuna":         {"atun"},
    "conejo":       {"rabbit"},
    "rabbit":       {"conejo"},
    "pescado":      {"fish"},
    "fish":         {"pescado"},
    "pato":         {"duck"},
    "duck":         {"pato"},
    "venado":       {"venison"},
    "venison":      {"venado"},
    "adulto":       {"adult"},
    "adult":        {"adulto"},
    "cachorro":     {"puppy", "junior"},
    "puppy":        {"cachorro", "junior"},
    "junior":       {"cachorro", "puppy"},
    "senior":       {"senior", "mature"},
    "esterilizado": {"sterilised", "sterilized", "neutered"},
    "sterilised":   {"esterilizado", "sterilized"},
    "sterilized":   {"esterilizado", "sterilised"},
    "sensible":     {"sensitive"},
    "sensitive":    {"sensible"},
    "ligero":       {"light"},
    "light":        {"ligero"},
    "grande":       {"maxi", "large"},
    "mediano":      {"medium"},
    "medium":       {"mediano"},
    "pequeño":      {"mini", "small"},
    "mini":         {"pequeño", "small"},
    "gatito":       {"kitten"},
    "kitten":       {"gatito"},
    "gato":         {"cat", "feline"},
    "cat":          {"gato", "feline"},
    "feline":       {"gato", "cat"},
    "perro":        {"dog", "canine"},
    "dog":          {"perro", "canine"},
    "canine":       {"perro", "dog"},
    "renal":        {"renal", "kidney"},
    "kidney":       {"renal"},
    "hepatico":     {"hepatic", "liver"},
    "hepatic":      {"hepatico"},
    "urinario":     {"urinary"},
    "urinary":      {"urinario"},
    "digestivo":    {"digestive", "gastrointestinal", "gastro"},
    "gastrointestinal": {"digestivo", "gastro"},
    "gastro":       {"digestivo", "gastrointestinal"},
    "articular":    {"joint", "mobility"},
    "joint":        {"articular", "mobility"},
    "mobility":     {"articular", "joint"},
    "hipoalergenico": {"hypoallergenic"},
    "hypoallergenic": {"hipoalergenico"},
    "recovery":     {"recovery", "recuperacion"},
    "obesity":      {"obesity", "obesidad", "weight"},
    "obesidad":     {"obesity", "weight"},
    "diabetes":     {"diabetes"},
    "premium":      {"premium"},
    "life":         {"life", "vida"},
    "verve":        {"verve"},
    "expert":       {"expert", "experto"},
    "nutrition":    {"nutrition", "nutricion"},
    "veterinary":   {"veterinaria", "vd", "vet"},
    "veterinaria":  {"veterinary", "vd", "vet"},
    "vd":           {"veterinary", "veterinaria"},
}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9\s]", " ", text)


def _tokenize(text: str) -> set:
    tokens = set(_normalize(text).split())
    tokens -= IGNORE_TOKENS
    tokens -= {t for t in tokens if len(t) <= 1}
    return tokens


def _expand(tokens: set) -> set:
    expanded = set(tokens)
    for t in tokens:
        expanded |= _SYNONYMS.get(t, set())
    return expanded


def find_best_match(shopify_title: str, catalog: dict) -> tuple:
    """
    Jaccard con sinónimos ES↔EN entre título Shopify y nombre+handle del catálogo.
    Devuelve (handle, score). Si score < MIN_SCORE → (None, score).
    """
    title_toks = _expand(_tokenize(shopify_title))
    best_handle, best_score = None, 0.0

    for handle, entry in catalog.items():
        cat_toks = _expand(
            _tokenize(handle.replace("-", " "))
            | _tokenize(entry.get("name", ""))
        )
        union = title_toks | cat_toks
        if not union:
            continue
        score = len(title_toks & cat_toks) / len(union)
        if score > best_score:
            best_score = score
            best_handle = handle

    if best_score < MIN_SCORE:
        return None, best_score
    return best_handle, best_score
