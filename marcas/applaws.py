"""
Scraper para Applaws — web oficial. Soporta dos sitios con CMS distintos:

  - España (WooCommerce ES): https://applaws.pet/producto/{slug}/
      slug en español con peso, p. ej. applaws-cat-dry-kitten-pollo-2kg.
      Resolución bajo demanda: slug directo + DDG + ranking del h1.

  - Reino Unido (Shopify EN): https://applaws.com/uk/products/{handle}/
      Es una tienda Shopify → se descarga el catálogo COMPLETO de una vez vía
      el endpoint público products.json (títulos, handles, imágenes, body_html,
      SKUs). El matching se hace localmente contra todos los títulos reales,
      sin DDG. El handle no lleva el peso (es una variante), p. ej.
      tuna-fillet-with-crab-in-broth-wet-cat-food.

El sitio activo se decide por el `web_url` que recibe scrape_catalog():
  applaws.com → UK (Shopify, inglés);  resto → ES (WooCommerce, español).

Como los títulos en Shopify (origen) están en español y el sitio UK en inglés,
para el sitio UK se traducen los términos del título (ES→EN) antes de puntuar.
El EAN/SKU se usa como señal exacta cuando está disponible.

applaws.pet bloquea peticiones sin navegador (HTTP 403) → Playwright + anti-bot.
applaws.com sirve products.json desde el CDN de Shopify; se navega con el
navegador ya "calentado" para esquivar el filtro de bots.

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
import os
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
        "shopify":   False,
    },
    "uk": {
        "lang":      "en",
        "base":      "https://applaws.com/uk/",
        "host_path": "applaws.com/uk/products/",
        "catalog":   Path("resultados/applaws_uk_catalog.json"),
        "threshold": 0.22,   # traducción imperfecta → umbral algo más bajo
        "slug_direct": False,
        "shopify":   True,
        # Endpoint público del catálogo Shopify (handles compartidos entre mercados).
        "json_base": "https://applaws.com",
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
    "un", "una", "al", "del", "en", "su", "sus",
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
    "langostinos": "prawn", "langostino": "prawn", "cangrejo": "crab",
    "queso": "cheese", "cerdo": "pork",
    "sobre": "pouch", "sobres": "pouch", "lata": "tin", "latas": "tin",
    "bolsa": "bag", "caldo": "broth", "gelatina": "jelly", "jalea": "jelly",
    "esparragos": "asparagus", "arroz": "rice", "verduras": "vegetable",
    "verdura": "vegetable", "vegetales": "vegetable", "calabaza": "pumpkin",
    "seco": "dry", "seca": "dry", "humedo": "wet", "humeda": "wet",
    "mojado": "wet", "mojada": "wet",
    "adulto": "adult", "adultos": "adult", "senior": "senior",
    "esterilizado": "sterilised", "esterilizada": "sterilised",
    "seleccion": "selection", "suprema": "supreme", "supremo": "supreme",
    "natural": "natural", "naturaleza": "nature", "arena": "litter",
    "multipack": "multipack", "comida": "food", "alimento": "food",
    "filete": "fillet", "filetes": "fillet", "trozos": "chunks",
    "trozo": "chunks", "tarrina": "pot", "tarrinas": "pot", "tarro": "pot",
    "receta": "recipe", "pate": "pate", "mousse": "mousse",
    "anchoas": "anchovy", "anchoa": "anchovy", "mar": "ocean",
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
                "/faq", "/privacy", "/terms", "/recipes", "/tag/",
                "/collections", "/collection/", "/policies", "/search")

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
    """Tokens del título Shopify, traducidos a EN si el sitio activo es UK.

    En UK el peso/formato (12x70, 70, 2, 400…) va en la variante, no en el
    título/handle del producto → se descartan los tokens numéricos para que no
    bajen el Jaccard contra el título inglés."""
    toks = _tokenize(title)
    if _ACTIVE["lang"] == "en":
        toks = _translate(toks)
        toks = {t for t in toks if not re.fullmatch(r"\d+(?:x\d+)?", t)}
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


# ─── Guard de especie (perro/gato) y etapa de vida (kitten/adulto/senior) ───────
# Los títulos Shopify usan las palabras inglesas DOG/CAT/KITTEN. applaws.com es
# mayoritariamente GATO; sin este guard, un producto DOG hereda la URL de un gato
# con el mismo sabor (p. ej. 'DOG ... POLLO CORDERO' → 'chicken-with-lamb-...-cat-food').

def _animal_of(tokens: set) -> str:
    if "dog" in tokens or "puppy" in tokens:
        return "dog"
    if "cat" in tokens or "kitten" in tokens:
        return "cat"
    return ""


def _stage_of(tokens: set) -> str:
    if "kitten" in tokens or "puppy" in tokens:
        return "junior"
    if "senior" in tokens:
        return "senior"
    if "adult" in tokens:
        return "adult"
    return ""


def _species_ok(title_tokens: set, cand_tokens: set) -> bool:
    """False si la especie del candidato choca con la del título. Un producto
    DOG solo casa con un handle que lleve marca 'dog' explícita (en applaws,
    web gato-first, un handle sin marca casi siempre es de gato)."""
    t = _animal_of(title_tokens)
    c = _animal_of(cand_tokens)
    if t == "dog":
        return c == "dog"
    return c != "dog"   # gato (o sin especie) nunca casa con un handle de perro


def _stage_penalty(title_tokens: set, cand_tokens: set) -> float:
    """Multiplicador <1 si la etapa de vida no coincide. Los productos kitten en
    applaws UK llevan 'kitten' en el handle → penaliza handles sin 'kitten'."""
    t = _stage_of(title_tokens)
    if t == "junior" and "kitten" not in cand_tokens:
        return 0.5
    c = _stage_of(cand_tokens)
    if t and c and t != c:
        return 0.5
    return 1.0


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

    # APPLAWS_HEADED=1 → navegador headed (bajo xvfb en CI) para esquivar el
    # filtro anti-bot que bloquea Chromium headless desde IPs de datacenter.
    headed = os.getenv("APPLAWS_HEADED", "").lower() in ("1", "true", "yes")
    launch_args = ["--no-sandbox", "--disable-setuid-sandbox",
                   "--disable-dev-shm-usage",
                   "--disable-blink-features=AutomationControlled"]
    if not headed:
        launch_args.append("--disable-gpu")
    log.info(f"  [playwright] headless={not headed}")
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=not headed, args=launch_args)
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


_CHALLENGE_HINTS = ("just a moment", "checking your browser",
                    "attention required", "cf-challenge", "cf_chl",
                    "enable javascript and cookies")


def _wait_challenge(page, max_wait: int = 25) -> bool:
    """Espera a que un reto JS de Cloudflare ('Just a moment…') se resuelva solo
    en el navegador headed (lo resuelve sin interacción). Devuelve True si la
    página ya NO es un reto."""
    waited = 0
    while waited < max_wait:
        try:
            html = (page.content() or "").lower()[:4000]
            title = (page.title() or "").lower()
        except Exception:
            html, title = "", ""
        if not any(h in html or h in title for h in _CHALLENGE_HINTS):
            return True
        page.wait_for_timeout(2000)
        waited += 2
    return False


def _warm_up(page):
    """Visita la home para establecer cookies y, si hay reto anti-bot
    (Cloudflare 'Just a moment…'), espera a que se resuelva y deje la cookie
    de clearance en el contexto."""
    if _PW["warmed"]:
        return
    try:
        resp = page.goto(_ACTIVE["base"], timeout=30000, wait_until="domcontentloaded")
        status = resp.status if resp else 0
        _wait_challenge(page)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        cookies = page.context.cookies()
        has_cf = any(c.get("name") == "cf_clearance" for c in cookies)
        log.info(f"  [warm-up] HTTP {status}, {len(cookies)} cookies, "
                 f"cf_clearance={'sí' if has_cf else 'no'}")
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


# ─── Catálogo Shopify (UK): sitemap + products.json ─────────────────────────────

def _log_block_diag(resp, url: str):
    """Loguea por qué un endpoint devolvió error (Cloudflare/Shopify/geo…)."""
    try:
        h = resp.headers or {}
        server = h.get("server", "?")
        cf = h.get("cf-ray") or h.get("cf-mitigated") or ""
        snippet = ""
        try:
            snippet = (resp.text() or "")[:160].replace("\n", " ")
        except Exception:
            pass
        log.info(f"  [bloqueo] HTTP {resp.status} {url} — server={server} "
                 f"cf={cf} body='{snippet}'")
    except Exception:
        pass


_PAGE_FETCH_JS = """
async (u) => {
    try {
        const r = await fetch(u, {credentials: 'include', redirect: 'follow'});
        return {status: r.status, body: await r.text()};
    } catch (e) {
        return {status: 0, body: '', err: String(e)};
    }
}
"""


def _fetch_raw(page, url: str):
    """Descarga el body crudo de una URL. Devuelve (texto, status).

    Método 1 (clave): fetch() DENTRO del contexto JS de la página ya calentada.
    Es una petición same-origin → usa las cookies cf_clearance y el fingerprint
    del navegador que YA superó el reto de Cloudflare en la home. El
    APIRequestContext, en cambio, tiene otro fingerprint y Cloudflare lo vuelve
    a retar aunque tenga la cookie (visto en producción: 403 'Just a moment').
    Método 2 (fallback): APIRequestContext."""
    # Método 1: fetch() en el contexto de la página (same-origin con cf_clearance).
    try:
        res = page.evaluate(_PAGE_FETCH_JS, url) or {}
        status = int(res.get("status") or 0)
        body = res.get("body") or ""
        low = body[:400].lower()
        challenged = any(h in low for h in _CHALLENGE_HINTS)
        if status and status < 400 and body and not challenged:
            return body, status
        log.info(f"  [fetch] HTTP {status} reto={challenged} {url}"
                 + (f" err={res.get('err')}" if res.get("err") else ""))
    except Exception as e:
        log.info(f"  _fetch_raw (page.fetch) error en {url}: {e}")
        status = 0

    # Método 2: navegar con el navegador y esperar a que el reto Cloudflare se
    # resuelva (la home demostró que el headed lo resuelve). Sirve para JSON/texto;
    # el visor XML de Chromium puede perder etiquetas en innerText.
    txt, st = _fetch_via_nav(page, url)
    if txt:
        return txt, st
    status = st or status

    # Método 3: APIRequestContext del contexto del navegador.
    try:
        resp = page.context.request.get(url, timeout=30000)
        if resp.status < 400:
            return resp.text(), resp.status
        _log_block_diag(resp, url)
        status = resp.status
    except Exception as e:
        log.info(f"  _fetch_raw (request) error en {url}: {e}")

    return None, status


def _fetch_via_nav(page, url: str):
    """Navega a la URL con el navegador real y espera a que el reto Cloudflare
    se resuelva solo; devuelve (texto, status) o (None, status)."""
    try:
        nav = page.goto(url, timeout=45000, wait_until="domcontentloaded")
        st = nav.status if nav else 0
    except Exception as e:
        log.info(f"  _fetch_via_nav goto error en {url}: {e}")
        return None, 0
    _wait_challenge(page, max_wait=10)
    try:
        txt = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
    except Exception:
        txt = ""
    low = txt[:400].lower()
    if not txt or any(h in low for h in _CHALLENGE_HINTS):
        log.info(f"  [nav] reto no resuelto / vacío en {url} (HTTP {st})")
        return None, (st or 403)
    return txt, 200


def _fetch_json(page, url: str):
    txt, status = _fetch_raw(page, url)
    if not txt:
        return None, status
    try:
        return json.loads(txt), status
    except Exception:
        return None, status


def _shopify_img_full(url: str) -> str:
    """Quita el sufijo de tamaño del CDN de Shopify para obtener el original."""
    return re.sub(
        r"_(?:\d+x\d*|grande|large|medium|small|compact|pico|icon|thumb|"
        r"original|master)(?=\.(?:jpg|jpeg|png|webp|gif)\b)",
        "", url, flags=re.IGNORECASE,
    )


_URL_BLOCK_RE = re.compile(r"<url>(.*?)</url>", re.S | re.I)
_LOC_RE       = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.I)
_IMG_LOC_RE   = re.compile(r"<image:loc>\s*([^<]+?)\s*</image:loc>", re.I)
_IMG_TITLE_RE = re.compile(
    r"<image:title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</image:title>", re.S | re.I)


def _parse_product_sitemap(xml: str) -> list:
    """Extrae (handle, título, imagen) de un sitemap de productos Shopify.
    El sitemap incluye <image:title> con el nombre real del producto."""
    out = []
    for block in _URL_BLOCK_RE.findall(xml):
        m = _LOC_RE.search(block)
        if not m or "/products/" not in m.group(1):
            continue
        loc = m.group(1).strip()
        handle = loc.rstrip("/").split("/products/")[-1].split("/")[0].split("?")[0]
        if not handle:
            continue
        t = _IMG_TITLE_RE.search(block)
        title = (t.group(1).strip() if t else "")
        im = _IMG_LOC_RE.search(block)
        img = (im.group(1).strip() if im else "")
        out.append((handle, title, img))
    return out


def _build_from_sitemap(catalog: dict) -> int:
    """Construye el catálogo desde el sitemap de Shopify (vía-preferida: los
    sitemaps suelen estar permitidos para bots). Devuelve nº de productos."""
    page = _get_page()
    if page is None:
        return 0
    _warm_up(page)
    base = _ACTIVE.get("json_base", "https://applaws.com")
    index, status = _fetch_raw(page, f"{base}/sitemap.xml")
    if not index:
        log.info(f"  sitemap.xml no accesible (HTTP {status})")
        return 0
    subs = re.findall(r"<loc>\s*([^<]*sitemap_products[^<]*)</loc>", index, re.I)
    if not subs:
        # sitemap.xml podría ser ya el de productos (tiendas pequeñas).
        subs = [f"{base}/sitemap.xml"] if "image:title" in index.lower() else []
    log.info(f"  sitemap: {len(subs)} sub-sitemaps de productos")
    total = 0
    for sm in subs:
        xmltxt, st = _fetch_raw(page, sm.strip())
        if not xmltxt:
            log.info(f"  sub-sitemap no accesible (HTTP {st}): {sm}")
            continue
        for handle, title, img in _parse_product_sitemap(xmltxt):
            if handle in catalog:
                continue
            catalog[handle] = {
                "name":         title or handle.replace("-", " "),
                "url":          f"{base}/uk/products/{handle}/",
                "images":       [_shopify_img_full(img)] if img else [],
                "body_html":    "",
                "skus":         [],
                "barcodes":     [],
                "handle":       handle,
                "product_type": "",
            }
            total += 1
        log.info(f"  sub-sitemap '{sm.split('/')[-1]}': acumulado {total}")
    return total


def _build_from_products_json(catalog: dict) -> int:
    """Construye el catálogo vía products.json (paginado). Devuelve nº productos."""
    page = _get_page()
    if page is None:
        return 0
    _warm_up(page)
    base = _ACTIVE.get("json_base", "https://applaws.com")
    total = 0
    for page_n in range(1, 21):   # tope de seguridad: 20 × 250 = 5000 productos
        url = f"{base}/products.json?limit=250&page={page_n}"
        data, status = _fetch_json(page, url)
        prods = (data or {}).get("products", []) if isinstance(data, dict) else []
        if not prods:
            log.info(f"  products.json pág {page_n}: vacío / HTTP {status} — fin")
            break
        for p in prods:
            handle = p.get("handle")
            if not handle:
                continue
            images = []
            for img in p.get("images", []):
                src = img.get("src") if isinstance(img, dict) else None
                if src and _should_keep_url(src):
                    images.append(_shopify_img_full(src))
            variants = p.get("variants", []) or []
            skus = [str(v.get("sku")).strip()
                    for v in variants if v.get("sku")]
            barcodes = [str(v.get("barcode")).strip()
                        for v in variants if v.get("barcode")]
            catalog[handle] = {
                "name":         p.get("title", "") or "",
                "url":          f"{base}/uk/products/{handle}/",
                "images":       images,
                "body_html":    p.get("body_html", "") or "",
                "skus":         skus,
                "barcodes":     barcodes,
                "handle":       handle,
                "product_type": p.get("product_type", "") or "",
            }
            total += 1
        log.info(f"  products.json pág {page_n}: {len(prods)} productos "
                 f"(acumulado {total})")
        if len(prods) < 250:
            break
    return total


def _build_shopify_catalog(catalog: dict) -> dict:
    """Catálogo completo del sitio Shopify (UK). Estrategia en cascada:
      1. sitemap de productos (handle + título + imagen) — vía bot-friendly.
      2. products.json (añade body_html/SKUs) — completa lo que falte.
    Indexa por handle."""
    n_sitemap = _build_from_sitemap(catalog)
    log.info(f"  Catálogo UK por sitemap: {n_sitemap} productos")
    n_json = _build_from_products_json(catalog)
    log.info(f"  Catálogo UK por products.json: {n_json} productos")
    log.info(f"  Catálogo UK (Shopify) construido: {len(catalog)} productos")
    return catalog


def _is_catalog_entry(entry) -> bool:
    """True solo para entradas del catálogo Shopify REAL (construido vía
    products.json/sitemap), que llevan campo 'handle' y título en inglés.

    Las entradas de la CACHÉ de resoluciones por producto —que find_best_match
    guarda con clave = título ES y SIN 'handle'— NO son catálogo y NUNCA deben
    entrar en el matching difuso: provocarían falsos positivos (p. ej. 'POLLO'
    puntúa 0.29 contra la entrada cacheada 'PESCADO Y SALMON' y heredaría su URL).
    Solo se usan para el cache-hit exacto por título en find_best_match."""
    return isinstance(entry, dict) and bool(entry.get("handle"))


def _has_real_catalog(catalog: dict) -> bool:
    """True si el dict contiene al menos una entrada del catálogo Shopify real."""
    return any(_is_catalog_entry(e) for e in catalog.values())


def _match_shopify_local(shopify_title: str, catalog: dict,
                         barcode: str = "") -> tuple:
    """Matching local contra el catálogo Shopify completo:
      1. EAN/SKU exacto (idioma-independiente) → score 1.0.
      2. Jaccard del título traducido vs título inglés → mejor candidato.
    Devuelve (handle, score) o (None, 0.0) si nada supera el umbral.

    Solo considera entradas del catálogo REAL (con 'handle'); ignora la caché
    de resoluciones por producto para no producir falsos positivos."""
    title_tokens = _title_tokens(shopify_title)

    if barcode:
        for handle, entry in catalog.items():
            if not _is_catalog_entry(entry):
                continue
            if (barcode in (entry.get("barcodes") or [])
                    or barcode in (entry.get("skus") or [])):
                log.info(f"  [EAN/SKU] {barcode} → {handle} "
                         f"('{entry.get('name','')}')")
                return handle, 1.0

    ranked = []
    for handle, entry in catalog.items():
        if not _is_catalog_entry(entry):
            continue
        cand = _tokenize(entry.get("name", ""))
        if not _species_ok(title_tokens, cand):
            continue
        score = _similarity(title_tokens, cand) * _stage_penalty(title_tokens, cand)
        ranked.append((score, handle, entry.get("name", "")))
    ranked.sort(reverse=True)

    for score, handle, name in ranked[:3]:
        log.info(f"  [shopify cand] score={score:.2f} '{name}' → {handle}")

    if ranked and ranked[0][0] >= _ACTIVE["threshold"]:
        score, handle, name = ranked[0]
        log.info(f"  ✓ Match local: '{name}' (score={score:.2f}) → {handle}")
        return handle, score

    if ranked:
        log.info(f"  Mejor local score={ranked[0][0]:.2f} < "
                 f"{_ACTIVE['threshold']} — sin match Shopify")
    return None, 0.0


def _resolve_uk_via_search(shopify_title: str, barcode: str = "") -> tuple:
    """Resuelve la URL UK puntuando el HANDLE de cada candidato de búsqueda,
    SIN navegar la página (Cloudflare bloquea la navegación desde datacenter,
    pero el handle ya viene en la URL del resultado). Devuelve (url, score).

    Shopify añade sufijos -2/-3/-4 a handles duplicados (multi-mercado); se
    quitan para puntuar y se prefiere el handle canónico (sin sufijo)."""
    title_tokens = _title_tokens(shopify_title)
    urls = _ddg_find_product_urls(shopify_title, barcode=barcode, max_urls=10)
    ranked = []
    for url in urls:
        handle = url.rstrip("/").split("/products/")[-1].split("/")[0].split("?")[0]
        if not handle:
            continue
        base = re.sub(r"-\d+$", "", handle)
        htoks = _tokenize(base.replace("-", " "))
        if not _species_ok(title_tokens, htoks):
            log.info(f"  [uk skip especie] {handle}")
            continue
        score = _similarity(title_tokens, htoks) * _stage_penalty(title_tokens, htoks)
        has_suffix = 1 if re.search(r"-\d+$", handle) else 0
        ranked.append((score, -has_suffix, url, handle))
    ranked.sort(reverse=True)
    for score, _suf, url, handle in ranked[:5]:
        log.info(f"  [uk cand] score={score:.2f} {handle}")
    if ranked and ranked[0][0] >= _ACTIVE["threshold"]:
        return ranked[0][2], ranked[0][0]
    return None, 0.0


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


def seed_uk_cache(title_to_url: dict) -> int:
    """Sincroniza la caché UK con URLs verificadas (título Shopify → URL), p. ej.
    las url_fabricante_2 corregidas a mano en Shopify. Las entradas se indexan por
    clave de título (sin 'handle') → find_best_match hace cache-hit EXACTO y
    devuelve la URL verificada sin volver a resolver (ni pasar por los guards),
    con source='shopify_manual'.

    Para cada (título, url): si hay URL la fija; si la URL viene vacía, **elimina**
    la entrada de la caché (así un producto cuyo url_fabricante_2 se borró en
    Shopify no conserva un valor obsoleto). Devuelve nº de URLs fijadas."""
    path = _SITES["uk"]["catalog"]
    cache: dict = {}
    if path.exists():
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    n = 0
    for title, url in title_to_url.items():
        key = _title_key(title)
        if url:
            cache[key] = {"name": title, "url": url, "images": [],
                          "source": "shopify_manual"}
            n += 1
        else:
            cache.pop(key, None)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return n


# ─── Interfaz pública ────────────────────────────────────────────────────────────

def scrape_catalog(web_url: str, rebuild: bool = False) -> dict:
    """Fija el sitio activo según web_url y carga/construye el catálogo.

    - ES (WooCommerce): catálogo bajo demanda (se rellena en find_best_match).
    - UK (Shopify): se descarga el catálogo COMPLETO vía products.json y se
      cachea. Si el endpoint falla, se cae al modo bajo demanda (DDG)."""
    global _ACTIVE
    _ACTIVE = _site_for(web_url)
    path = _ACTIVE["catalog"]
    log.info(f"Sitio activo: {_ACTIVE['lang'].upper()} ({_ACTIVE['base']}) "
             f"— catálogo {path}")

    if rebuild and path.exists():
        path.unlink()
        log.info("Catálogo borrado (rebuild)")

    if path.exists():
        try:
            catalog = json.loads(path.read_text(encoding="utf-8"))
            log.info(f"Catálogo cargado desde caché: {len(catalog)} entradas")
            return catalog
        except Exception:
            pass

    if _ACTIVE.get("shopify"):
        log.info("Construyendo catálogo Shopify completo (products.json)…")
        catalog = _build_shopify_catalog({})
        if catalog:
            _save_catalog(catalog)
        else:
            log.warning("products.json no devolvió productos — se usará DDG "
                        "bajo demanda como fallback")
        return catalog

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

    # UK Shopify: 1) catálogo local si se pudo construir; 2) si no, resolver por
    # el HANDLE de los resultados de búsqueda (sin navegar → Cloudflare bloquea
    # la navegación desde las IPs de datacenter de Actions).
    if _ACTIVE.get("shopify"):
        if title_key in catalog and catalog[title_key].get("url"):
            log.info(f"  Match caché: {title_key}")
            return title_key, 1.0
        # Matching difuso SOLO si existe catálogo Shopify real (handles ingleses).
        # Si solo hay caché de resoluciones por producto, NO se usa: cada producto
        # debe resolver su propia URL por búsqueda (evita heredar la URL de otro).
        if _has_real_catalog(catalog):
            handle, score = _match_shopify_local(shopify_title, catalog, barcode)
            if handle is not None:
                return handle, score
            log.info("  Sin match en catálogo Shopify — búsqueda por handle")
        url, score = _resolve_uk_via_search(shopify_title, barcode)
        if url:
            # Entrada de CACHÉ (sin 'handle'): solo para cache-hit exacto por
            # título; nunca entra en el matching difuso (ver _is_catalog_entry).
            catalog[title_key] = {"name": shopify_title, "url": url,
                                  "images": [], "resolved": True}
            _save_catalog(catalog)
            log.info(f"  ✓ Resuelto (handle de búsqueda): score={score:.2f} {url}")
            return title_key, score
        log.warning(f"  Sin resolución UK para '{shopify_title}'")
        return None, 0.0

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
