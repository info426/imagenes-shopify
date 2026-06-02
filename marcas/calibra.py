"""
Scraper para Calibra — mycalibra.es / mycalibra.eu / calibra.cat.
Usa Playwright porque los tres dominios devuelven 403 a requests directos.

Interfaz estándar (requerida por core/process_brand.py):
  scrape_catalog(web_url, rebuild=False) -> dict
  find_best_match(shopify_title, catalog) -> (handle, score)
"""

import json
import logging
import os
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

try:
    from core import image_match
except Exception:  # pragma: no cover — degrada a solo-texto si falta el módulo
    image_match = None

log = logging.getLogger(__name__)

# ─── Pesos y umbrales del matching combinado texto + imagen ───────────────────
# La foto del producto Shopify actúa como segundo eje ("Google Lens"): confirma
# o RECHAZA el candidato textual. Evita los falsos positivos forzados del run #15
# (ROCKETS → calibra-rockets, JOY CHEWY → dental-brushes, DOG LATA → URL de gato).
_W_TEXT = 0.45            # peso del Jaccard de texto en el score combinado
_W_IMG  = 0.55            # peso de la similitud visual (discrimina mejor)
_TEXT_ONLY_MIN = 0.30     # sin señal visual → exige texto más alto que MIN_SCORE
_TEXT_TRUST    = 0.55     # texto tan alto que una imagen débil no debe rechazar
_ACCEPT_COMBINED = 0.45   # score combinado mínimo para aceptar

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

# Navegador headed (bajo xvfb) cuando el workflow lo pide — más resistente al gate
# anti-bot de Calibra ("Ověření přístupu") desde IPs de datacenter (Actions).
# Default: headless. El workflow Resolver URLs ya exporta APPLAWS_HEADED=1 + xvfb.
_HEADED = (os.getenv("CALIBRA_HEADED") or os.getenv("APPLAWS_HEADED") or "") \
    in ("1", "true", "True")


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
                r'https?://[^\s"\' <>\\]+\.(?:jpg|jpeg|png|webp)[^\s"\' <>\\]*', text
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


def _fetch_bytes_via_page(page, url: str) -> bytes | None:
    """Descarga bytes usando el contexto del navegador ya calentado (cookies +
    fingerprint que superó el gate anti-bot). Imprescindible para el CDN de
    imágenes de mycalibra, que devuelve 403 a peticiones sin navegador."""
    try:
        resp = page.context.request.get(url, timeout=30000)
        if resp.ok:
            return resp.body()
        log.debug(f"    [img] HTTP {resp.status} {url}")
    except Exception as e:
        log.debug(f"    [img] fetch via page falló {url}: {e}")
    return None


def _attach_img_feature(page, entry: dict):
    """Calcula y guarda en el catálogo la huella visual (CLIP o hash) de la
    primera imagen utilizable del producto. No-op si image_match está deshabilitado."""
    if image_match is None or not getattr(image_match, "ENABLED", False):
        return
    for u in (entry.get("images") or [])[:3]:
        data = _fetch_bytes_via_page(page, u)
        feat = image_match.compute_feature(data) if data else None
        if feat:
            entry["img_feat"] = feat
            entry["img_feat_url"] = u
            return


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

            entry = {
                "name":   name,
                "url":    prod_url,
                "source": source["lang"],
                "images": images,
            }
            # Precomputar la huella visual AHORA, con el navegador vivo: el CDN de
            # imágenes de mycalibra da 403 sin navegador, así que en match-time no
            # se podría descargar. Se guarda en el catálogo (cache permanente).
            _attach_img_feature(page, entry)
            catalog[slug] = entry
            feat_tag = " +img" if entry.get("img_feat") else ""
            log.info(f"    {slug}: {len(images)} img{feat_tag} — {name[:60]}")
            time.sleep(0.8)

        except Exception as e:
            log.warning(f"    Error en {prod_url}: {e}")

    return catalog


# ─── Interfaz pública: scrape_catalog ────────────────────────────────────────

def _norm_host(url: str) -> str:
    """Host sin 'www.' para comparar el web_url pedido con los dominios de SOURCES."""
    host = urlparse(url).netloc.lower() if url else ""
    return host[4:] if host.startswith("www.") else host


def _source_for_web_url(web_url: str):
    """Devuelve la fuente de SOURCES cuyo dominio coincide con web_url, o None."""
    host = _norm_host(web_url)
    if not host:
        return None
    for src in SOURCES:
        if _norm_host(src["base"]) == host:
            return src
    return None


def _catalog_path_for(source) -> Path:
    """Caché por sitio para no mezclar las URLs de campo 1 (mycalibra.es) y campo 2
    (mycalibra.eu / calibra.cat). Sin fuente concreta → caché legacy (todas)."""
    if source is None:
        return CATALOG_PATH
    return Path(f"resultados/calibra_{source['lang']}_catalog.json")


def scrape_catalog(web_url: str = "", rebuild: bool = False) -> dict:
    """
    Scrapea el catálogo de Calibra con Playwright y devuelve
    { slug: { name, url, source, images: [url, ...] } }.

    web_url SELECCIONA el sitio a resolver, para poder rellenar dos metacampos
    distintos con el mismo scraper (el workflow corre una vez por metacampo):
      - mycalibra.es  → url_fabricante   (campo 1, español)
      - mycalibra.eu  → url_fabricante_2 (campo 2, inglés; misma estructura /{slug})
      - calibra.cat   → soportado también si expone /{slug} (hoy es portal de marca)
    Si web_url apunta a una fuente conocida se scrapea SOLO ese dominio y se cachea
    en resultados/calibra_{lang}_catalog.json (las URLs devueltas son siempre del
    sitio pedido). Si web_url está vacío o no coincide → legacy: todas las fuentes
    en resultados/calibra_catalog.json.
    """
    source     = _source_for_web_url(web_url)
    sources    = [source] if source else SOURCES
    cache_path = _catalog_path_for(source)

    if rebuild and cache_path.exists():
        cache_path.unlink()
        log.info(f"Catálogo borrado (rebuild): {cache_path.name}")
    elif cache_path.exists():
        try:
            catalog = json.loads(cache_path.read_text(encoding="utf-8"))
            log.info(f"Catálogo cargado desde caché ({cache_path.name}): "
                     f"{len(catalog)} productos")
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
    site_label = source["base"] if source else "todas las fuentes"
    log.info(f"Scraping catálogo Calibra ({site_label}) con Playwright "
             f"[{'headed' if _HEADED else 'headless'}]...")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not _HEADED,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"],
        )
        ctx = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="es-ES",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={
                "Accept-Language": HEADERS["Accept-Language"],
                "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                           "image/avif,image/webp,image/apng,*/*;q=0.8"),
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            },
            ignore_https_errors=True,
        )
        # Ocultar señales de automatización para pasar el gate anti-bot de Calibra
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "Object.defineProperty(navigator,'languages',{get:()=>['es-ES','es','en']});"
            "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
            "window.chrome={runtime:{}};"
        )
        page = ctx.new_page()

        for src in sources:
            log.info(f"\n=== Fuente: {src['base']} ===")
            # Warm-up: visitar la home para que el challenge anti-bot emita la
            # cookie de acceso antes de crawlear las categorías.
            try:
                page.goto(src["base"], timeout=30000,
                          wait_until="domcontentloaded")
                page.wait_for_timeout(4000)
            except Exception as e:
                log.warning(f"  warm-up {src['base']} falló: {e}")
            try:
                entries = _scrape_source(page, src)
                # Solo añadir productos no vistos en fuentes anteriores
                new_count = 0
                for slug, entry in entries.items():
                    if slug not in catalog:
                        catalog[slug] = entry
                        new_count += 1
                log.info(f"  → {new_count} productos nuevos de {src['base']}")
            except Exception as e:
                log.warning(f"  Error scraping {src['base']}: {e}")

        browser.close()

    cache_path.parent.mkdir(exist_ok=True)
    cache_path.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(f"\nCatálogo guardado: {len(catalog)} productos → {cache_path}")
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


def _text_scores(shopify_title: str, catalog: dict) -> list:
    """Lista [(handle, entry, text_score)] con el Jaccard texto de cada entrada."""
    title_toks = _expand(_tokenize(shopify_title))
    out = []
    for handle, entry in catalog.items():
        if not isinstance(entry, dict):
            continue
        cat_toks = _expand(
            _tokenize(handle.replace("-", " "))
            | _tokenize(entry.get("name", ""))
        )
        union = title_toks | cat_toks
        score = len(title_toks & cat_toks) / len(union) if union else 0.0
        out.append((handle, entry, score))
    return out


def find_best_match(shopify_title: str, catalog: dict,
                    barcode: str = "", product_images: list = None) -> tuple:
    """
    Matching combinado: Jaccard de texto (ES↔EN) + reconocimiento por imagen.

    Si se reciben las imágenes del producto Shopify (product_images) y el catálogo
    tiene huellas visuales precomputadas (img_feat), la foto actúa como segundo eje
    tipo Google Lens:
      - score combinado = _W_TEXT·texto + _W_IMG·imagen
      - confirma matches correctos aunque el texto sea flojo (foto idéntica/igual)
      - RECHAZA candidatos cuando la imagen dice claramente "no es el mismo producto"
        (img ≤ WEAK y texto bajo) → el producto queda sin_match en vez de forzar
        una URL errónea (caso ROCKETS / JOY CHEWY / DOG-LATA→gato del run #15).

    Sin imágenes o sin huellas en el catálogo → solo texto, con umbral elevado
    (_TEXT_ONLY_MIN) para no arrastrar la basura de score 0.12-0.30.
    Devuelve (handle, score). (None, score) si no hay match fiable.
    """
    scored = _text_scores(shopify_title, catalog)
    if not scored:
        return None, 0.0

    has_feats = any(e.get("img_feat") for _, e, _ in scored)
    use_img = (image_match is not None and getattr(image_match, "ENABLED", False)
               and bool(product_images) and has_feats)

    if not use_img:
        handle, entry, t = max(scored, key=lambda x: x[2])
        if t < _TEXT_ONLY_MIN:
            log.info(f"  solo-texto: mejor score={t:.2f} < {_TEXT_ONLY_MIN} → sin_match")
            return None, t
        return handle, t

    # Huellas visuales de las fotos del producto Shopify (CDN público → requests)
    q_feats = []
    for u in (product_images or [])[:3]:
        f = image_match.compute_feature_from_url(u)
        if f:
            q_feats.append(f)
    if not q_feats:
        log.info("  [img] no pude obtener la huella de la imagen Shopify → solo texto")
        handle, entry, t = max(scored, key=lambda x: x[2])
        return (handle, t) if t >= _TEXT_ONLY_MIN else (None, t)

    bk = image_match.backend()
    STRONG, WEAK = image_match.thresholds()
    log.info(f"  [img] backend={bk} STRONG={STRONG} WEAK={WEAK} "
             f"({len(q_feats)} foto(s) Shopify)")

    # Similitud visual de cada candidato con la foto Shopify
    cands = []  # (handle, text, img)  img=-1 si el candidato no tiene huella
    for handle, entry, t in scored:
        cf = entry.get("img_feat")
        img = image_match.best_similarity(q_feats, cf) if cf else -1.0
        cands.append((handle, t, img))

    # 1) Confirmación visual fuerte (foto prácticamente idéntica) — vale para AMBOS
    #    backends. Es el caso más fiable: la misma foto del fabricante en Shopify.
    strong = [c for c in cands if c[2] >= STRONG]
    if strong:
        handle, t, img = max(strong, key=lambda c: (c[2], c[1]))
        log.info(f"  [img] CONFIRMADO visualmente: '{handle}' img={img:.2f} texto={t:.2f}")
        return handle, max(t, img)

    # 2) CLIP: combina texto+imagen y RECHAZA si la imagen contradice al texto.
    if bk == "clip":
        handle, t, img = max(
            cands, key=lambda c: _W_TEXT * c[1] + _W_IMG * max(c[2], 0.0)
        )
        combined = _W_TEXT * t + _W_IMG * max(img, 0.0)
        img_str = f"{img:.2f}" if img >= 0 else "—"
        log.info(f"  [img] mejor: '{handle}' texto={t:.2f} img={img_str} "
                 f"combinado={combined:.2f}")
        if 0 <= img <= WEAK and t < _TEXT_TRUST:
            log.info(f"  [img] RECHAZO: img={img:.2f} ≤ WEAK y texto={t:.2f} "
                     f"< {_TEXT_TRUST} → sin_match (evita falso positivo)")
            return None, combined
        if combined >= _ACCEPT_COMBINED:
            return handle, combined
        log.info(f"  combinado={combined:.2f} < {_ACCEPT_COMBINED} → sin_match")
        return None, combined

    # 3) hash (fallback): sin confirmación fuerte la imagen no es fiable para
    #    discriminar → decide el texto con umbral elevado (nunca peor que antes).
    handle, t, img = max(cands, key=lambda c: c[1])
    if t < _TEXT_ONLY_MIN:
        log.info(f"  [img-hash] sin confirmación visual y texto={t:.2f} "
                 f"< {_TEXT_ONLY_MIN} → sin_match")
        return None, t
    return handle, t
