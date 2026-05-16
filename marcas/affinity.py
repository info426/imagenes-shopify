"""
Scraper para AFFINITY — agrupa 6 sub-marcas:
  ADVANCE / ADVANCE VET  → advance-affinity.com/es/es/
  LIBRA                  → libra-affinity.com/es/es/
  BREKKIES               → brekkies-affinity.com/es/es/
  NATURAL TRAINER        → naturaltrainer.com/es/es/
  NATURE'S VARIETY       → naturesvariety.com/es/

Interfaz estándar (requerida por core/process_brand.py):
  scrape_catalog(web_url, rebuild=False) -> dict
  find_best_match(shopify_title, catalog) -> (handle, score)
  get_ddg_query(shopify_title) -> str
"""

import json
import logging
import re
import time
import unicodedata
from pathlib import Path

log = logging.getLogger(__name__)

CATALOG_PATH = Path("resultados/affinity_catalog.json")
MIN_SCORE    = 0.12

# ─── Sub-marca y configuración de URLs ────────────────────────────────────────

BRAND_CONFIG = {
    "advance": {
        "base": "https://www.advance-affinity.com",
        "categories": [
            "https://www.advance-affinity.com/es/es/perro",
            "https://www.advance-affinity.com/es/es/gato",
        ],
        "link_must_contain": ["/comida-perro/", "/comida-gato/"],
        "link_must_not_contain": ["veterinary", "consejos", "#", "javascript:", "mailto:"],
        "min_segments": 5,
        "link_require_html": True,
    },
    "advance_vet": {
        "base": "https://www.advance-affinity.com",
        "categories": [
            "https://www.advance-affinity.com/es/es/perro/dietas-veterinarias",
            "https://www.advance-affinity.com/es/es/gato/dietas-veterinarias",
        ],
        "link_must_contain": ["veterinary"],
        "link_must_not_contain": ["#", "javascript:", "mailto:", "consejos"],
        "min_segments": 5,
        "link_require_html": True,
    },
    "libra": {
        "base": "https://www.libra-affinity.com",
        "categories": [
            "https://www.libra-affinity.com/es/es/comida-perro/",
            "https://www.libra-affinity.com/es/es/comida-gato/",
        ],
        "link_must_contain": ["/comida-perro/", "/comida-gato/"],
        "link_must_not_contain": ["#", "javascript:", "mailto:", "/donde-comprar", "/legal", "/nutricion"],
        "min_segments": 4,
        "link_require_html": True,
    },
    "brekkies": {
        "base": "https://brekkies-affinity.com",
        "categories": [
            "https://brekkies-affinity.com/es/es/perro",
            "https://brekkies-affinity.com/es/es/gato",
        ],
        "link_must_contain": ["/es/es/perro/", "/es/es/gato/"],
        "link_must_not_contain": ["#", "javascript:", "mailto:", "/saber-mas/", "consejos"],
        "min_segments": 4,
        "link_require_html": True,
    },
    "natural_trainer": {
        "base": "https://www.naturaltrainer.com",
        "categories": [
            "https://www.naturaltrainer.com/es/es/perro/",
            "https://www.naturaltrainer.com/es/es/gato/",
        ],
        "link_must_contain": [],
        "link_must_not_contain": ["javascript:", "mailto:", "/ingredientes", "/alimentacion-",
                                   "/homepage", "/my-natural-trainer", "/legal", "/saber-mas",
                                   "consejos", "/perro/", "/gato/"],
        "min_segments": 3,
        "link_require_html": True,
    },
    "natures_variety": {
        "base": "https://www.naturesvariety.com",
        "categories": [
            "https://www.naturesvariety.com/es/perros/",
            "https://www.naturesvariety.com/es/gatos/",
        ],
        "link_must_contain": ["/es/perros/", "/es/gatos/"],
        "link_must_not_contain": ["#", "javascript:", "mailto:"],
        "min_segments": 3,
        "link_require_html": False,
    },
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

# ─── Detección de sub-marca ────────────────────────────────────────────────────

def _detect_sub_brand(title: str) -> str:
    t = title.upper().strip()
    if t.startswith(("ADVANCE VET", "ADVANCE DIETS", "ADVANCE VETERINARY")):
        return "advance_vet"
    if t.startswith(("ADVANCE", "AD ")):
        return "advance"
    if t.startswith("BREKKIES"):
        return "brekkies"
    if t.startswith("LIBRA"):
        return "libra"
    if t.startswith(("NATURAL TRAINER", "NATURAL TRAI")):
        return "natural_trainer"
    if t.startswith(("NATURE'S VARIETY", "NATURE'S V", "NATURES VARIETY")):
        return "natures_variety"
    return "unknown"


# ─── DDG query builder ─────────────────────────────────────────────────────────

_TITLE_NOISE = re.compile(r'\s*\((?:NDR|PV|NV|ONLINE)\)\s*', re.IGNORECASE)

_BRAND_NAMES = {
    "advance":       "Advance",
    "advance_vet":   "Advance Veterinary Diets",
    "libra":         "Libra",
    "brekkies":      "Brekkies",
    "natural_trainer": "Natural Trainer",
    "natures_variety": "Nature's Variety",
}


def get_ddg_query(shopify_title: str) -> str:
    """Builds an optimized DuckDuckGo image query for an AFFINITY product."""
    clean = _TITLE_NOISE.sub(" ", shopify_title).strip()
    sub_brand = _detect_sub_brand(shopify_title)

    if sub_brand != "unknown":
        brand_name = _BRAND_NAMES[sub_brand]
        first_word = brand_name.split()[0].upper()
        if not clean.upper().startswith(first_word):
            clean = f"{brand_name} {clean}"
    else:
        # "AD " prefix is the AFFINITY abbreviation for Advance
        if re.match(r'^AD\s', clean, re.IGNORECASE):
            clean = "Advance " + clean[3:].strip()

    return clean + " product image"


# ─── Extracción de imágenes (genérica) ────────────────────────────────────────

def _bump_resolution(url: str) -> str:
    """Intenta pedir la máxima resolución a CDNs comunes."""
    url_clean = re.sub(r'_\d+x(\d*)\.', '.', url)
    if "sw=" not in url_clean and "w=" not in url_clean:
        sep = "&" if "?" in url_clean else "?"
        url_clean = f"{url_clean}{sep}w=2000"
    return url_clean


def _extract_images(page) -> list:
    """Extrae URLs de imágenes del producto de la página actual (múltiples estrategias)."""
    urls: set = set()

    # 1. <img> con src grande (evitar iconos)
    try:
        for el in page.query_selector_all("img[src]"):
            src = el.get_attribute("src") or ""
            if not src or src.startswith("data:"):
                continue
            try:
                w = page.evaluate("el => el.naturalWidth", el) or 0
                if w > 0 and w < 200:
                    continue
            except Exception:
                pass
            if any(kw in src.lower() for kw in ("/product", "/products", "/catalog",
                                                  "product-image", "produit",
                                                  "package", "food", "/media/",
                                                  "wp-content/uploads")):
                full = src if src.startswith("http") else f"https:{src}" if src.startswith("//") else src
                urls.add(_bump_resolution(full))
    except Exception:
        pass

    # 2. srcset — tomar la URL más grande (último elemento suele ser mayor)
    try:
        for el in page.query_selector_all("img[srcset]"):
            srcset = el.get_attribute("srcset") or ""
            parts  = [p.strip() for p in srcset.split(",") if p.strip()]
            if parts:
                candidate = parts[-1].split()[0]
                if candidate and not candidate.startswith("data:"):
                    full = candidate if candidate.startswith("http") else f"https:{candidate}"
                    urls.add(_bump_resolution(full))
    except Exception:
        pass

    # 3. data-src / lazy loading
    for attr in ("data-src", "data-lazy-src", "data-original", "data-zoom-image"):
        try:
            for el in page.query_selector_all(f"img[{attr}]"):
                src = el.get_attribute(attr) or ""
                if src and not src.startswith("data:"):
                    full = src if src.startswith("http") else f"https:{src}"
                    urls.add(_bump_resolution(full))
        except Exception:
            pass

    # 4. JSON-LD (schema.org)
    try:
        ld_texts = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
                        .map(s => s.textContent || '');
        }""")
        for text in (ld_texts or []):
            try:
                data = json.loads(text)
                imgs = data.get("image", [])
                if isinstance(imgs, str):
                    imgs = [imgs]
                for img in imgs:
                    if isinstance(img, str) and img.startswith("http"):
                        urls.add(_bump_resolution(img))
                    elif isinstance(img, dict):
                        url_field = img.get("url") or img.get("contentUrl") or ""
                        if url_field:
                            urls.add(_bump_resolution(url_field))
            except Exception:
                pass
    except Exception:
        pass

    # 5. Variables JS con URLs de imágenes (WooCommerce, Magento, etc.)
    try:
        js_texts = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('script:not([src])'))
                        .map(s => s.textContent || '')
                        .filter(t => t.includes('product_image') || t.includes('gallery_image')
                                  || t.includes('"image"') || t.includes("'image'"));
        }""")
        for text in (js_texts or []):
            for m in re.finditer(r'https?://[^\s"\'<>]+\.(?:jpg|jpeg|png|webp)[^\s"\'<>]*', text):
                url = m.group(0).rstrip("\\,;")
                if len(url) < 400:
                    urls.add(_bump_resolution(url))
    except Exception:
        pass

    return list(urls)


def _find_product_links(page, config: dict) -> set:
    """Recoge hrefs de la página que parecen ser páginas de producto."""
    base          = config["base"]
    must_have     = config.get("link_must_contain", [])
    must_not      = config.get("link_must_not_contain", [])
    min_segs      = config.get("min_segments", 3)
    require_html  = config.get("link_require_html", False)

    found: set = set()
    try:
        for el in page.query_selector_all("a[href]"):
            href = el.get_attribute("href") or ""
            if not href:
                continue

            # Convertir a absoluta y verificar dominio
            if href.startswith("/"):
                href = base + href
            elif href.startswith("http"):
                if not href.startswith(base):
                    continue
            else:
                continue

            # Filtro de extensión HTML (para sitios Affinity-platform)
            if require_html and not href.split("?")[0].endswith(".html"):
                continue

            # Filtros de contenido
            if any(x in href for x in must_not):
                continue
            if must_have and not any(x in href for x in must_have):
                continue

            # Mínimo de segmentos de ruta (evitar páginas de categoría cortas)
            path = href.rstrip("/").split("?")[0]
            segments = [s for s in path.split("/") if s]
            if len(segments) < min_segs:
                continue

            found.add(href.split("?")[0])
    except Exception as e:
        log.debug(f"  _find_product_links error: {e}")

    return found


# ─── Scraping por marca ────────────────────────────────────────────────────────

def _scrape_brand(page, brand_key: str, config: dict) -> dict:
    """Scrapea todos los productos de una sub-marca y devuelve entradas de catálogo."""
    brand_catalog: dict = {}

    for cat_url in config["categories"]:
        log.info(f"  [{brand_key}] Cargando: {cat_url}")
        try:
            page.goto(cat_url, timeout=45000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(2000)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(500)
        except Exception as e:
            log.warning(f"  [{brand_key}] Error cargando {cat_url}: {e}")
            continue

        product_urls = _find_product_links(page, config)
        log.info(f"  [{brand_key}] {len(product_urls)} productos encontrados en {cat_url}")

        for prod_url in sorted(product_urls):
            slug = prod_url.rstrip("/").split("/")[-1].replace(".html", "")
            handle = f"{brand_key}__{slug}"

            if handle in brand_catalog:
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
                    or page.query_selector("h1")
                )
                name   = name_el.inner_text().strip() if name_el else slug
                images = _extract_images(page)

                brand_catalog[handle] = {
                    "name":   name,
                    "url":    prod_url,
                    "brand":  brand_key,
                    "images": images,
                }
                log.info(f"    {handle}: {len(images)} imagen(es) — {name[:50]}")
                time.sleep(0.8)

            except Exception as e:
                log.warning(f"    Error en {prod_url}: {e}")

    return brand_catalog


# ─── Interfaz pública: scrape_catalog ─────────────────────────────────────────

def scrape_catalog(web_url: str, rebuild: bool = False) -> dict:
    """
    Scrapea las 6 webs de AFFINITY con Playwright y construye un catálogo unificado.
    Devuelve: { handle: { name, url, brand, images: [url, ...] } }
    Cachea en resultados/affinity_catalog.json.
    El parámetro web_url se ignora (las URLs están hardcodeadas por sub-marca).
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
        log.error("Playwright no instalado: pip install playwright && playwright install chromium")
        return {}

    catalog: dict = {}
    n_brands = len(BRAND_CONFIG)
    log.info(f"Scraping catálogo AFFINITY con Playwright ({n_brands} sub-marcas)...")

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

        for brand_key, config in BRAND_CONFIG.items():
            log.info(f"\n=== Sub-marca: {brand_key.upper()} ===")
            try:
                brand_entries = _scrape_brand(page, brand_key, config)
                catalog.update(brand_entries)
                log.info(f"  → {len(brand_entries)} productos scrapeados")
            except Exception as e:
                log.warning(f"  Error scraping {brand_key}: {e}")

        browser.close()

    CATALOG_PATH.parent.mkdir(exist_ok=True)
    CATALOG_PATH.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(f"\nCatálogo guardado: {len(catalog)} productos → {CATALOG_PATH}")
    return catalog


# ─── Matching ─────────────────────────────────────────────────────────────────

_SYNONYMS: dict[str, set] = {
    "pollo":       {"chicken"},
    "chicken":     {"pollo"},
    "cordero":     {"lamb"},
    "lamb":        {"cordero"},
    "arroz":       {"rice"},
    "rice":        {"arroz"},
    "salmon":      {"salmon"},
    "salmón":      {"salmon"},
    "ternera":     {"beef", "veal"},
    "buey":        {"ox", "beef"},
    "beef":        {"ternera", "buey"},
    "veal":        {"ternera"},
    "pavo":        {"turkey"},
    "turkey":      {"pavo"},
    "atun":        {"tuna"},
    "tuna":        {"atun"},
    "conejo":      {"rabbit"},
    "rabbit":      {"conejo"},
    "ganso":       {"goose"},
    "goose":       {"ganso"},
    "aves":        {"poultry", "chicken", "bird"},
    "poultry":     {"aves"},
    "pescado":     {"fish"},
    "fish":        {"pescado"},
    "adulto":      {"adult"},
    "adult":       {"adulto"},
    "cachorro":    {"puppy"},
    "puppy":       {"cachorro"},
    "senior":      {"senior"},
    "esterilizado": {"sterilized", "sterilised"},
    "sterilized":  {"esterilizado"},
    "sterilised":  {"esterilizado"},
    "sensitivo":   {"sensitive"},
    "sensit":      {"sensitive"},
    "sensitive":   {"sensitivo"},
    "ligero":      {"light"},
    "light":       {"ligero"},
    "grande":      {"maxi", "large"},
    "mediano":     {"medium"},
    "medium":      {"mediano"},
    "pequeno":     {"mini", "small"},
    "gatito":      {"kitten"},
    "kitten":      {"gatito"},
    "feline":      {"cat", "gato"},
    "gato":        {"feline", "cat"},
    "cat":         {"feline", "gato"},
    "canine":      {"dog", "perro"},
    "perro":       {"canine", "dog"},
    "dog":         {"canine", "perro"},
    "renal":       {"renal"},
    "articular":   {"articular", "joint"},
    "joint":       {"articular"},
    "urinario":    {"urinary"},
    "urinary":     {"urinario"},
    "gastro":      {"gastro", "gastrointestinal"},
    "gastrointestinal": {"gastro"},
    "hipoalergenico": {"hypoallergenic"},
    "hypoallergenic": {"hipoalergenico"},
    "atopico":     {"atopic"},
    "atopic":      {"atopico"},
    "hepatico":    {"hepatic"},
    "hepatic":     {"hepatico"},
}

IGNORE_TOKENS = {
    "affinity", "advance", "libra", "brekkies", "naturaltrainer",
    "natural", "trainer", "natures", "variety", "nature",
    "ndr", "pv", "nv", "online",
    "de", "el", "la", "los", "las", "con", "sin", "y",
    "kg", "gr", "g", "lb", "x", "ml",
    "a", "e", "o",
}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return text


def _tokenize(text: str) -> set:
    tokens = set(_normalize(text).split())
    tokens -= IGNORE_TOKENS
    tokens -= {t for t in tokens if len(t) <= 1}
    return tokens


def _expand_synonyms(tokens: set) -> set:
    expanded = set(tokens)
    for t in tokens:
        expanded |= _SYNONYMS.get(t, set())
    return expanded


def find_best_match(shopify_title: str, catalog: dict) -> tuple:
    """
    Jaccard entre tokens (con sinónimos ES↔EN) del título Shopify y nombre+handle
    de productos del catálogo, filtrado a la sub-marca detectada.
    Devuelve (handle, score). Si score < MIN_SCORE → (None, score).
    """
    sub_brand  = _detect_sub_brand(shopify_title)
    title_toks = _expand_synonyms(_tokenize(shopify_title))

    best_handle, best_score = None, 0.0

    for handle, entry in catalog.items():
        entry_brand = entry.get("brand", "")
        if sub_brand != "unknown" and entry_brand and entry_brand != sub_brand:
            continue

        cat_toks = _expand_synonyms(
            _tokenize(handle.replace("__", " ").replace("-", " "))
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
