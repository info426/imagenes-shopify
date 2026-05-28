"""
Scraper para Applaws — web oficial. Soporta dos sitios:

  - España (WooCommerce ES): https://applaws.pet/producto/{slug}/
      slug en español con peso, p. ej. applaws-cat-dry-kitten-pollo-2kg
  - Reino Unido (EN):        https://applaws.com/uk/...
      títulos/slug en inglés (chicken, breast, pouch...)

El sitio activo se decide por el `web_url` que recibe scrape_catalog():
  applaws.com → UK (inglés);  resto → ES (español).

Como los títulos en Shopify están en español y el sitio UK en inglés, para el
sitio UK se traducen los términos del título (ES→EN) antes de buscar/puntuar y
se prioriza la búsqueda por EAN (independiente del idioma).

applaws.pet/applaws.com bloquean peticiones sin navegador real (HTTP 403), por
lo que toda la navegación se hace con Playwright + anti-bot completo.

Interfaz estándar (core/process_brand.py):
  scrape_catalog(web_url, rebuild=False) -> dict
  find_best_match(shopify_title, catalog, barcode="") -> (handle, score)
  scrape_product_url(url, barcode="") -> {name, url, images} | None
  title_cache_key(title) -> str
  get_ddg_query(shopify_title) -> str
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

MIN_SCORE = 0.10

# ─── Configuración por sitio ────────────────────────────────────────────────────

_SITES = {
    "es": {
        "lang":      "es",
        "base":      "https://applaws.pet/",
        "host_path": "applaws.pet/producto/",
        "catalog":   Path("resultados/applaws_catalog.json"),
        "threshold": 0.30,
        "slug_direct": True,
    },
    "uk": {
        "lang":      "en",
        "base":      "https://applaws.com/uk/",
        "host_path": "applaws.com/uk/",
        "catalog":   Path("resultados/applaws_uk_catalog.json"),
        "threshold": 0.22,   # traducción imperfecta → umbral algo más bajo
        "slug_direct": False,
    },
}

# Sitio activo (lo fija scrape_catalog según web_url). Por defecto ES.
_ACTIVE = _SITES["es"]


def _site_for(web_url: str) -> dict:
    return _SITES["uk"] if "applaws.com" in (web_url or "").lower() else _SITES["es"]


# Notas internas que la tienda añade a los títulos: *DX*, (NDR), (PV)...
_TITLE_NOISE = re.compile(r'\s*(?:\*[^*]*\*|\((?:NDR|PV|NV|ONLINE)\))\s*', re.IGNORECASE)

IGNORE_TOKENS = {
    "applaws",
    # stopwords ES
    "de", "el", "la", "los", "las", "con", "sin", "y", "e", "o", "a", "para",
    "un", "una", "al", "del",
    # stopwords EN
    "with", "and", "the", "in", "of", "for", "to",
    # unidades
    "ml", "gr", "g", "kg", "mg", "cl", "l", "cm", "mm", "x", "ud", "uds", "pack",
    "dx",
}

# Traducción de términos del dominio (ES → EN) para el sitio UK.
_ES_EN = {
    "gato": "cat", "gatos": "cat", "gatito": "kitten", "gatitos": "kitten",
    "perro": "dog", "perros": "dog", "cachorro": "puppy", "cachorros": "puppy",
    "pollo": "chicken", "pechuga": "breast", "muslo": "thigh",
    "salmon": "salmon", "atun": "tuna", "pescado": "fish", "sardina": "sardine",
    "sardinas": "sardine", "caballa": "mackerel", "trucha": "trout",
    "cordero": "lamb", "ternera": "beef", "buey": "beef", "vacuno": "beef",
    "res": "beef", "pavo": "turkey", "pato": "duck", "conejo": "rabbit",
    "jamon": "ham", "higado": "liver", "gambas": "prawn", "gamba": "prawn",
    "langostinos": "prawn", "cangrejo": "crab", "queso": "cheese",
    "sobre": "pouch", "sobres": "pouch", "lata": "tin", "latas": "tin",
    "bolsa": "bag", "caldo": "broth", "gelatina": "jelly", "jalea": "jelly",
    "esparragos": "asparagus", "arroz": "rice", "verduras": "vegetable",
    "verdura": "vegetable", "calabaza": "pumpkin",
    "seco": "dry", "seca": "dry", "humedo": "wet", "humeda": "wet",
    "adulto": "adult", "adultos": "adult", "senior": "senior",
    "esterilizado": "sterilised", "esterilizada": "sterilised",
    "seleccion": "selection", "suprema": "supreme", "supremo": "supreme",
    "natural": "natural", "naturaleza": "nature", "arena": "litter",
    "multipack": "multipack", "comida": "food", "alimento": "food",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# URLs UK que no son ficha de producto.
_NON_PRODUCT = ("/blog", "/news", "/pages", "/page/", "/cart", "/checkout",
                "/account", "/category", "/product-category", "/brand",
                "/where-to-buy", "/stockist", "/contact", "/about",
                "/faq", "/privacy", "/terms", "/recipes", "/tag/")

_PW = {"pw": None, "browser": None, "ctx": None, "page": None, "warmed": False}


# ─── Utilidades de texto ──────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"(\d+)\s*(ml|gr|kg|mg|cl|l|cm|mm|g)\b", r"\1 \2", text)
    return text


def _clean_title(title: str) -> str:
    return _TITLE_NOISE.sub(" ", title).strip()


def _title_key(title: str) -> str:
    norm = _normalize(_clean_title(title))
    return re.sub(r"\s+", "-", norm.strip()).strip("-")


def _direct_slug(title: str) -> str:
    norm = unicodedata.normalize("NFD", _clean_title(title).lower())
    ascii_str = norm.encode("ascii", "ignore").decode()
    slug = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", ascii_str)).strip("-")
    if slug and not slug.startswith("applaws"):
        slug = "applaws-" + slug
    return slug


def _translate(tokens: set) -> set:
    """Traduce tokens ES→EN (solo se usa para el sitio UK)."""
    return {_ES_EN.get(t, t) for t in tokens}


def _tokenize(text: str) -> set:
    tokens = set(_normalize(text).split())
    tokens -= IGNORE_TOKENS
    tokens -= {t for t in tokens if len(t) <= 1}
    return tokens


def _title_tokens(title: str) -> set:
    """Tokens del título Shopify, traducidos a EN si el sitio activo es UK."""
    toks = _tokenize(title)
    if _ACTIVE["lang"] == "en":
        toks = _translate(toks)
    return toks


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
    locale = "en-GB" if _ACTIVE["lang"] == "en" else "es-ES"
    accept_lang = ("en-GB,en;q=0.9,es;q=0.8" if _ACTIVE["lang"] == "en"
                   else "es-ES,es;q=0.9,en;q=0.8")
    ctx = browser.new_context(
        user_agent=USER_AGENT,
        locale=locale,
        viewport={"width": 1440, "height": 900},
        extra_http_headers={
            "Accept-Language": accept_lang,
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
        Object.defineProperty(navigator, 'languages', {get: () => ['en-GB', 'en', 'es']});
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
    if _PW["warmed"]:
        return
    try:
        page.goto(_ACTIVE["base"], timeout=20000, wait_until="domcontentloaded")
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
    return re.sub(r'-\d+x\d+(\.[a-zA-Z]{3,4})(?:\?.*)?$', r'\1', url.split("?")[0])


# ─── Extracción de imágenes ─────────────────────────────────────────────────────

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

    try:
        for sel in ("meta[property='og:image']",
                    "meta[property='og:image:secure_url']",
                    "meta[name='twitter:image']"):
            el = page.query_selector(sel)
            if el:
                _add(el.get_attribute("content") or "")
    except Exception:
        pass

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


def _translate_text(text: str) -> str:
    """Traduce palabra a palabra ES→EN (para construir queries del sitio UK)."""
    out = []
    for w in _normalize(text).split():
        if w in IGNORE_TOKENS:
            continue
        out.append(_ES_EN.get(w, w))
    return " ".join(out)


def _is_product_url(url: str) -> bool:
    low = url.lower()
    if _ACTIVE["host_path"] not in low:
        return False
    if any(seg in low for seg in _NON_PRODUCT):
        return False
    # debe haber algo después de host_path (no la home /uk/)
    tail = low.split(_ACTIVE["host_path"], 1)[-1].strip("/")
    return len(tail) > 0


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
    """URLs candidatas del sitio activo. Cascada distinta por idioma."""
    seen: set = set()
    urls: list = []
    host_path = _ACTIVE["host_path"]
    clean = _clean_title(title)

    if _ACTIVE["lang"] == "en":
        # UK: EAN primero (idioma-independiente), luego título traducido
        if barcode:
            q0 = f"site:applaws.com {barcode}"
            log.info(f"  [DDG EAN] {q0}")
            urls += _ddg_query_urls(q0, max_urls - len(urls), seen)
        en = _translate_text(clean)
        q1 = f"site:{host_path} {en}"
        log.info(f"  [DDG EN] {q1}")
        urls += _ddg_query_urls(q1, max_urls - len(urls), seen)
        if len(urls) < max_urls:
            q2 = f"applaws uk {en}"
            log.info(f"  [DDG EN libre] {q2}")
            urls += _ddg_query_urls(q2, max_urls - len(urls), seen)
    else:
        q1 = f"site:{host_path} {clean}"
        log.info(f"  [DDG] {q1}")
        urls += _ddg_query_urls(q1, max_urls - len(urls), seen)
        no_brand = re.sub(r'^APPLAWS\s+', '', clean, flags=re.IGNORECASE).strip()
        if len(urls) < max_urls and no_brand != clean:
            q2 = f"site:{host_path} {no_brand}"
            log.info(f"  [DDG sin marca] {q2}")
            urls += _ddg_query_urls(q2, max_urls - len(urls), seen)
        if len(urls) < 2 and barcode:
            q3 = f"site:{host_path} {barcode}"
            log.info(f"  [DDG EAN] {q3}")
            urls += _ddg_query_urls(q3, max_urls - len(urls), seen)

    log.info(f"  [DDG] {len(urls)} candidatos: {urls}")
    return urls


# ─── Persistencia del catálogo ──────────────────────────────────────────────────

def _save_catalog(catalog: dict):
    try:
        path = _ACTIVE["catalog"]
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except Exception as e:
        log.warning(f"No se pudo guardar catálogo: {e}")


save_catalog = _save_catalog


# ─── Interfaz pública ────────────────────────────────────────────────────────────

def scrape_catalog(web_url: str, rebuild: bool = False) -> dict:
    """Fija el sitio activo según web_url y carga el catálogo cacheado de ese sitio."""
    global _ACTIVE
    _ACTIVE = _site_for(web_url)
    path = _ACTIVE["catalog"]
    log.info(f"Sitio activo: {_ACTIVE['lang'].upper()} ({_ACTIVE['base']}) "
             f"— catálogo {path}")
    if rebuild and path.exists():
        path.unlink()
        log.info("Catálogo borrado (rebuild) — se reconstruirá bajo demanda")
        return {}
    if path.exists():
        try:
            catalog = json.loads(path.read_text(encoding="utf-8"))
            log.info(f"Catálogo cargado desde caché: {len(catalog)} entradas")
            return catalog
        except Exception:
            pass
    log.info("Catálogo vacío — se rellenará bajo demanda")
    return {}


def scrape_product_url(url: str, barcode: str = "") -> dict | None:
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
    Resolución por producto en el sitio activo:
      1. Cache hit por clave de título normalizada.
      2. (ES) Slug directo applaws.pet/producto/<slug>/.
      3. DDG (ES: título; UK: EAN + título traducido) → ranking del h1.
    Extrae imágenes solo del candidato ganador. Devuelve (handle, score).
    """
    title_tokens = _title_tokens(shopify_title)
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

    if _ACTIVE["slug_direct"]:
        direct = _direct_slug(shopify_title)
        if direct:
            durl = f"{_ACTIVE['base']}producto/{direct}/"
            name = _fetch_name(page, durl)
            if name:
                candidates.append((durl, name))
                seen.add(durl)
                log.info(f"  [slug directo] {durl} → '{name}'")

    for url in _ddg_find_product_urls(shopify_title, barcode=barcode):
        if url in seen:
            continue
        name = _fetch_name(page, url)
        if name:
            candidates.append((url, name))
            seen.add(url)

    best = None   # (score, url, name)
    for url, name in candidates:
        score = _similarity(title_tokens, _tokenize(name))
        log.info(f"  [cand] score={score:.2f} '{name}' {url}")
        if best is None or score > best[0]:
            best = (score, url, name)

    if best and best[0] >= _ACTIVE["threshold"]:
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
        log.warning(f"  Mejor candidato score={best[0]:.2f} < {_ACTIVE['threshold']} "
                    f"— descartado: '{best[2]}'")
    log.warning(f"  Sin resolución para '{shopify_title}'")
    return None, 0.0
