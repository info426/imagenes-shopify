"""
Scraper para Farmina — farmina.com/es

CMS: PrestaShop con URLs /es/eshop/{categoria}/{subcategoria}/{id}-{slug}.html
Una página web por receta (independientemente del tamaño del paquete).
Los productos Shopify llevan el peso en el título (2.5 KG, 7 KG, 12 KG) → se
elimina antes del matching para lograr correspondencia muchos-a-uno (varios
tamaños de saco → misma URL de receta).

Dos submarcas comparten el mismo dominio:
  - N&D (Natural & Delicious) — vendor "Farmina" en Shopify
  - Vet Life                   — vendor "Farmina Vet Life" en Shopify
Ambas usan la misma caché (resultados/farmina_catalog.json).

Interfaz estándar (requerida por core/process_brand.py):
  scrape_catalog(web_url, rebuild=False) -> dict
  find_best_match(shopify_title, catalog, barcode="") -> (handle, score)
  scrape_product_url(url, barcode="") -> dict | None
"""

import atexit
import json
import logging
import os
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

try:
    from core import image_match
except Exception:  # pragma: no cover — degrada a solo-texto si falta el módulo
    image_match = None

log = logging.getLogger(__name__)

CATALOG_PATH    = Path("resultados/farmina_catalog.json")
PRODUCT_DOMAIN  = "farmina.com"
MATCH_THRESHOLD = 0.25       # umbral de texto cuando NO hay filtro de imagen
# Con el gate de imagen activo, la foto es el filtro de precisión → se acepta
# texto más flojo (títulos Farmina escuetos vs nombres web largos con "& GRANADA").
_TEXT_MIN_WITH_IMG = 0.12
# Nº de candidatos del catálogo (mejor texto) que se visitan para el gate de imagen.
_TOPK = 8

# Prefijo de marca y submarca a eliminar del título Shopify antes del matching.
# Cubre variantes: "FARMINA ND", "FARMINA N&D", "FARMINA VET LIFE", "ND", etc.
_BRAND_RE = re.compile(
    r'^(?:FARMINA\s+)?(?:N(?:&|AND)D\s+|ND\s+|VET\s+LIFE\s+|VETLIFE\s+)',
    re.IGNORECASE,
)

# Peso al final o en el interior del título: "2.5 KG", "7 KG", "1,5 KG", "400 G"
_WEIGHT_RE = re.compile(
    r'\b\d+(?:[.,]\d+)?\s*(?:kg|g|gr|lb)\b', re.IGNORECASE
)

IGNORE_TOKENS = {
    "farmina", "nd", "vet", "life", "vetlife",
    "de", "el", "la", "los", "las", "con", "sin", "y", "para",
    "e", "o", "al", "un", "una", "a",
    "kg", "g", "gr", "lb", "ml", "l", "x",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Headed bajo xvfb cuando el workflow lo activa (igual que calibra/menforsan)
_HEADED = (os.getenv("FARMINA_HEADED") or os.getenv("APPLAWS_HEADED") or "") \
    in ("1", "true", "True")

_PW = {"pw": None, "browser": None, "ctx": None, "page": None}


# ─── Utilidades de texto ──────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9\s]", " ", text)


def _strip_weight(title: str) -> str:
    """Elimina tokens de peso del título ('2.5 KG', '400 G', '1,5 KG')."""
    return _WEIGHT_RE.sub("", title).strip()


def _clean_for_match(title: str) -> str:
    """Elimina prefijo de marca y peso. Resultado se usa en matching y DDG."""
    t = _BRAND_RE.sub("", title.strip())
    return _strip_weight(t).strip()


def _title_key(title: str) -> str:
    """Clave de caché estable sin peso → varios tamaños del mismo producto
    comparten la misma entrada y la misma URL de receta.
    Ejemplo: 'FARMINA ND OCEAN PERRO MINI BACALAO 2.5 KG'
             → 'ocean-perro-mini-bacalao'
    """
    norm = _normalize(_clean_for_match(title))
    norm = re.sub(r'\s+', ' ', norm).strip()
    return re.sub(r"\s+", "-", norm).strip("-")


def _tokenize(text: str) -> set:
    tokens = set(_normalize(text).split())
    tokens -= IGNORE_TOKENS
    tokens -= {t for t in tokens if len(t) <= 1}
    return tokens


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _stem_set(tokens: set) -> set:
    return {_stem(t) for t in tokens}


# Sinónimos EN↔ES: los títulos Shopify mezclan inglés (DOG, PUPPY, CHICKEN) con
# español, y la web farmina.com/es está en español (CACHORRO, POLLO, LATA).
_SYNONYMS = {
    "dog": {"perro"}, "perro": {"dog"},
    "cat": {"gato"}, "gato": {"cat"},
    "puppy": {"cachorro"}, "cachorro": {"puppy"},
    "kitten": {"gatito"}, "gatito": {"kitten"},
    "adult": {"adulto"}, "adulto": {"adult"},
    "chicken": {"pollo"}, "pollo": {"chicken"},
    "lamb": {"cordero"}, "cordero": {"lamb"},
    "fish": {"pescado", "arenque"}, "pescado": {"fish"},
    "cod": {"bacalao"}, "bacalao": {"cod"},
    "herring": {"arenque"}, "arenque": {"herring"},
    "boar": {"jabali"}, "jabali": {"boar"},
    "pork": {"cerdo"}, "cerdo": {"pork"},
    "lata": {"can", "wet", "humedo"}, "can": {"lata"}, "humedo": {"lata", "wet"},
    "caja": {"lata", "can", "wet"},  # CAJA 6X285GR = caja de latas → formato húmedo
    "pomegranate": {"granada"}, "granada": {"pomegranate"},
    "pumpkin": {"calabaza"}, "calabaza": {"pumpkin"},
    "mini": {"small", "pequeno"}, "maxi": {"large", "grande"},
    "renal": {"renal"}, "neutered": {"neutered", "esterilizado"},
}


def _expand(tokens: set) -> set:
    out = set(tokens)
    for t in tokens:
        out |= _SYNONYMS.get(t, set())
    return out


# ─── Guard de especie (perro ≠ gato) ─────────────────────────────────────────
_DOG_M = {"dog", "dogs", "perro", "perros", "canine", "canino"}
_CAT_M = {"cat", "cats", "gato", "gatos", "feline", "felino", "kitten", "gatito"}


def _species_of_text(text: str) -> str:
    toks = set(_normalize(text).split())
    d, c = bool(toks & _DOG_M), bool(toks & _CAT_M)
    return "dog" if (d and not c) else ("cat" if (c and not d) else "")


def _species_of_url(url: str) -> str:
    u = (url or "").lower()
    if "alimento-para-perros" in u or "canine" in u:
        return "dog"
    if "alimento-para-gatos" in u or "feline" in u:
        return "cat"
    return ""


def _species_ok(title_sp: str, cand_url: str, cand_name: str) -> bool:
    """Incompatible solo si ambas especies son conocidas y distintas."""
    cs = _species_of_url(cand_url) or _species_of_text(cand_name)
    return not (title_sp and cs and title_sp != cs)


def _similarity(a_tokens: set, b_tokens: set) -> float:
    a = _stem_set(_expand(a_tokens))
    b = _stem_set(_expand(b_tokens))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ─── Playwright lazy init ─────────────────────────────────────────────────────

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
            log.error("Playwright no instalado: pip install playwright && "
                      "playwright install chromium")
            return None

    browser = pw.chromium.launch(
        headless=not _HEADED,
        args=["--no-sandbox", "--disable-setuid-sandbox",
              "--disable-dev-shm-usage", "--disable-gpu"],
    )
    ctx = browser.new_context(
        user_agent=USER_AGENT,
        locale="es-ES",
        viewport={"width": 1440, "height": 900},
        extra_http_headers={
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,image/apng,*/*;q=0.8",
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
    _PW.update({"browser": browser, "ctx": ctx, "page": page})

    def _cleanup():
        try:
            browser.close()
        except Exception:
            pass
    atexit.register(_cleanup)

    try:
        page.goto("https://www.farmina.com/es/", timeout=20000,
                  wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        status = page.evaluate("() => document.readyState")
        log.info(f"[warm-up] farmina.com/es OK (state={status})")
    except Exception as e:
        log.debug(f"[warm-up] farmina.com/es: {e}")

    return page


# ─── Filtrado de URLs de producto ─────────────────────────────────────────────

def _is_product_url(url: str) -> bool:
    """Acepta solo páginas de producto farmina.com/es/eshop/…/{id}-{slug}.html"""
    if PRODUCT_DOMAIN not in url:
        return False
    path = urlparse(url).path
    if "/es/eshop/" not in path:
        return False
    last = path.rstrip("/").rsplit("/", 1)[-1]
    return bool(re.match(r'^\d+-.+\.html$', last, re.IGNORECASE))


# ─── Extracción de imágenes ───────────────────────────────────────────────────

def _should_keep_url(url: str) -> bool:
    low = url.lower()
    return not any(kw in low for kw in (
        ".svg", "logo", "icon", "banner", "sprite", "placeholder",
        "favicon", "loader", "spinner", "flag", "pixel",
    ))


def _upgrade_prestashop_url(url: str) -> str:
    return re.sub(
        r'/(\d+)-(?:cart|small|medium|home|category)_default/',
        r'/\1-large_default/', url
    )


def _extract_images(page, page_url: str) -> list:
    ordered: list = []
    seen: set = set()

    def _add(raw_url: str):
        if not raw_url or raw_url.startswith("data:"):
            return
        full = urljoin(page_url, raw_url)
        if not full.startswith("http") or not _should_keep_url(full):
            return
        full = _upgrade_prestashop_url(full)
        clean = re.sub(r'-\d+x\d+(\.[a-zA-Z]{3,4})$', r'\1', full.split("?")[0])
        if clean in seen:
            return
        seen.add(clean)
        ordered.append(full)

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

    try:
        ld_texts = page.evaluate("""() => {
            return Array.from(
                document.querySelectorAll('script[type="application/ld+json"]'))
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
                .filter(img => {
                    if (img.closest('.related') || img.closest('.upsells') ||
                        img.closest('.cross-sells') ||
                        img.closest('[class*="related"]') ||
                        img.closest('[class*="upsell"]') ||
                        img.closest('footer') || img.closest('header') ||
                        img.closest('nav')) return false;
                    if (img.closest('.product-miniature') ||
                        img.closest('.js-product-miniature') ||
                        img.closest('[class*="miniature"]') ||
                        img.closest('[class*="product-list"]') ||
                        img.closest('[class*="products-grid"]')) return false;
                    if (img.naturalWidth > 0 && img.naturalWidth < 200) return false;
                    return true;
                })
                .map(img => ({
                    srcset:         img.getAttribute('srcset')           || '',
                    dataSrc:        img.getAttribute('data-src')         || '',
                    dataLazySrc:    img.getAttribute('data-lazy-src')    || '',
                    dataLargeImage: img.getAttribute('data-large_image') || '',
                    dataZoomImage:  img.getAttribute('data-zoom-image')  || '',
                    src:            img.getAttribute('src')              || ''
                }));
        }""")
        log.info(f"    DOM imgs (excl. relacionados): {len(img_data or [])}")
        for item in (img_data or []):
            srcset = item.get("srcset", "")
            parts = [p.strip() for p in srcset.split(",") if p.strip()]
            if parts:
                _add(parts[-1].split()[0])
            for key in ("dataSrc", "dataLazySrc", "dataLargeImage",
                        "dataZoomImage", "src"):
                _add(item.get(key, ""))
    except Exception as e:
        log.info(f"    DOM imgs error: {e}")

    log.info(f"    Imágenes extraídas: {len(ordered)}")
    return ordered


def _try_url(page, url: str) -> tuple:
    """Navega a url y devuelve (h1_name, images). (None, []) si falla."""
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
            log.info(f"  h1 vacío en {url}")
            return None, []
        images = _extract_images(page, page.url)
        return name, images
    except Exception as e:
        log.debug(f"  _try_url error: {e}")
        return None, []


# ─── Búsqueda DDG ─────────────────────────────────────────────────────────────

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
                    url = url.split("?")[0].split("#")[0]
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


def _ddg_find_product_urls(title: str, max_urls: int = 6) -> list:
    """Cascada DDG: 1) título limpio, 2) versión corta (primeros 4 tokens)."""
    seen: set = set()
    urls: list = []
    clean = _clean_for_match(title)

    q1 = f"site:farmina.com/es/eshop/ {clean}"
    log.info(f"  [DDG] {q1}")
    urls += _ddg_query_urls(q1, max_urls - len(urls), seen)

    if len(urls) < 2:
        short_tokens = _normalize(clean).split()[:4]
        short = " ".join(short_tokens)
        if short and short != _normalize(clean):
            q2 = f"site:farmina.com/es/eshop/ {short}"
            log.info(f"  [DDG fallback] {q2}")
            urls += _ddg_query_urls(q2, max_urls - len(urls), seen)

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
        log.warning(f"No se pudo guardar catálogo Farmina: {e}")


def save_catalog(catalog: dict):
    _save_catalog(catalog)


# ─── Interfaz pública ─────────────────────────────────────────────────────────

def _slug_of(url: str) -> str:
    """Último segmento de la URL de producto (clave del catálogo)."""
    return unquote(url).rstrip("/").split("?")[0].split("/")[-1]


def _name_from_slug(url: str) -> str:
    """Nombre legible desde el slug: '484-pollo-&-granada-cachorro-lata-.html'
    → 'pollo & granada cachorro lata'."""
    seg = _slug_of(url)
    seg = re.sub(r"\.html?$", "", seg, flags=re.IGNORECASE)
    seg = re.sub(r"^\d+[-_]", "", seg)            # quitar el id PrestaShop
    return re.sub(r"[-_]+", " ", seg).strip()


def _parse_sitemap_locs(xml_text: str) -> list:
    if not xml_text:
        return []
    return [m.strip() for m in re.findall(r"<loc>\s*(.*?)\s*</loc>",
                                          xml_text, re.IGNORECASE | re.DOTALL)]


def _fetch_text(page, url: str) -> str:
    """Texto crudo de una URL (robots.txt / sitemap XML).
    1) APIRequestContext (rápido). 2) NAVEGACIÓN + cuerpo de la respuesta: usa el
    fingerprint del navegador (pasa anti-bot) y `resp.text()` da el cuerpo crudo;
    a diferencia de fetch() in-page NO está sujeto a la CSP connect-src de la página
    (que en el run #26 devolvía vacío)."""
    data = _fetch_bytes_via_page(page, url)
    if data:
        try:
            return data.decode("utf-8", "ignore")
        except Exception:
            pass
    try:
        resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
        status = resp.status if resp else "?"
        if resp and resp.ok:
            try:
                return resp.text()
            except Exception:
                return page.content() or ""
        log.info(f"    [sitemap] {url} → HTTP {status}")
    except Exception as e:
        log.info(f"    [sitemap] navegación {url} falló: {e}")
    return ""


def _collect_sitemap_links(page) -> set:
    """Descubre URLs de producto desde el/los sitemap(s) de farmina.com.
    PrestaShop: robots.txt declara el sitemap; índices `{shop}_{lang}_0_sitemap.xml`."""
    found: set = set()
    seen: set = set()
    queue = [
        "https://www.farmina.com/robots.txt",   # declara el sitemap real
        "https://www.farmina.com/sitemap.xml",
        "https://www.farmina.com/es/sitemap.xml",
        "https://www.farmina.com/1_es_0_sitemap.xml",
        "https://www.farmina.com/2_es_0_sitemap.xml",
        "https://www.farmina.com/1_en_0_sitemap.xml",
    ]
    depth = 0
    while queue and depth < 6:
        depth += 1
        nxt = []
        for sm in queue:
            if sm in seen:
                continue
            seen.add(sm)
            text = _fetch_text(page, sm)
            if not text:
                log.info(f"    [sitemap] {sm} → vacío/inaccesible")
                continue
            # robots.txt: extraer las líneas Sitemap:
            if sm.endswith("robots.txt"):
                sms = [ln.split(":", 1)[1].strip() for ln in text.splitlines()
                       if ln.lower().startswith("sitemap:")]
                log.info(f"    [robots] declara {len(sms)} sitemap(s): {sms}")
                nxt += sms
                continue
            locs = _parse_sitemap_locs(text)
            prods = [l for l in locs if _is_product_url(l)]
            maps = [l for l in locs if l.lower().endswith(".xml")
                    or "sitemap" in l.rsplit("/", 1)[-1].lower()]
            log.info(f"    [sitemap] {sm} → {len(locs)} locs ({len(prods)} producto, "
                     f"{len(maps)} sub-sitemaps)")
            for l in prods:
                found.add(l.split("?")[0])
            nxt += maps
        queue = nxt
    log.info(f"  Sitemap Farmina: {len(found)} URLs de producto")
    return found


def scrape_catalog(web_url: str, rebuild: bool = False) -> dict:
    """
    Construye el catálogo COMPLETO de farmina.com/es desde el sitemap
    { slug: { name, url } } y lo cachea. El matching es local en find_best_match
    (sin depender de DDG). Soporta 'Farmina' (N&D) y 'Farmina Vet Life' (misma caché).
    """
    if rebuild and CATALOG_PATH.exists():
        CATALOG_PATH.unlink()
        log.info("Catálogo Farmina borrado (rebuild)")
    elif CATALOG_PATH.exists():
        try:
            catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
            log.info(f"Catálogo Farmina cargado desde caché: {len(catalog)} entradas")
            return catalog
        except Exception:
            pass

    page = _get_page()
    if page is None:
        log.warning("Sin navegador — find_best_match caerá a DDG")
        return {}

    catalog: dict = {}
    for url in sorted(_collect_sitemap_links(page)):
        slug = _slug_of(url)
        if slug and slug not in catalog:
            catalog[slug] = {"name": _name_from_slug(url), "url": url}

    if catalog:
        _save_catalog(catalog)
        log.info(f"Catálogo Farmina: {len(catalog)} productos del sitemap")
    else:
        log.warning("Sitemap vacío/inaccesible — find_best_match usará DDG por producto")
    return catalog


# Alias público para backfill de metacampos (mapea título Shopify → clave caché)
title_cache_key = _title_key


def scrape_product_url(url: str, barcode: str = "") -> dict | None:
    """Extrae imágenes de una URL exacta de producto Farmina (sin DDG).
    Habilita el override por metacampo fuentes.url_fabricante."""
    page = _get_page()
    if page is None:
        return None
    name, images = _try_url(page, url)
    if not images:
        return None
    return {"name": name or "", "url": url, "images": images}


def _fetch_bytes_via_page(page, url: str) -> bytes | None:
    """Bytes de imagen vía el contexto del navegador (cookies/anti-bot)."""
    try:
        resp = page.context.request.get(url, timeout=30000)
        if resp.ok:
            return resp.body()
    except Exception as e:
        log.debug(f"  [img] fetch via page falló {url}: {e}")
    return None


def _shopify_feats(product_images: list) -> list:
    """Huellas visuales de las fotos del producto Shopify (CDN público)."""
    if image_match is None or not getattr(image_match, "ENABLED", False):
        return []
    feats = []
    for u in (product_images or [])[:3]:
        f = image_match.compute_feature_from_url(u)
        if f:
            feats.append(f)
    return feats


def _candidate_feat(page, images: list):
    """Huella visual de la imagen principal del candidato (vía navegador)."""
    if image_match is None or not getattr(image_match, "ENABLED", False):
        return None
    for u in (images or [])[:2]:
        data = _fetch_bytes_via_page(page, u)
        feat = image_match.compute_feature(data) if data else None
        if feat:
            return feat
    return None


# Caché en memoria de resoluciones (title_key → score) para el many-to-one:
# varias tallas (2.5/7/12 KG) de la misma receta resuelven una sola vez por run.
_RESOLVED: dict = {}


def _has_real_catalog(catalog: dict) -> bool:
    """True si el catálogo trae entradas del sitemap (clave = slug con /eshop/)."""
    return any(isinstance(e, dict) and "/eshop/" in (e.get("url") or "")
               for e in catalog.values())


def _catalog_candidates(page, title: str, catalog: dict, q_feats: list) -> list:
    """Rankea TODO el catálogo por texto y visita los top-K para bajar su imagen.
    Candidatos deterministas y completos (del sitemap), sin depender de DDG."""
    clean = _tokenize(_clean_for_match(title))
    title_sp = _species_of_text(title)
    ranked = []
    for slug, e in catalog.items():
        url = e.get("url") if isinstance(e, dict) else None
        if not url or "/eshop/" not in url:
            continue
        if not _species_ok(title_sp, url, e.get("name", "")):
            continue  # guard de especie: perro nunca casa con gato
        cat_toks = _tokenize(e.get("name", "")) | _tokenize(slug.replace("-", " "))
        ranked.append((_similarity(clean, cat_toks), url, e))
    ranked.sort(key=lambda x: x[0], reverse=True)

    cands = []  # (text, name, images, url, cand_feat)
    for tscore, url, e in ranked[:_TOPK]:
        if tscore <= 0:
            break
        name, images = _try_url(page, url)
        name = name or e.get("name", "")
        score = max(tscore, _similarity(clean, _tokenize(name)))
        cf = _candidate_feat(page, images) if q_feats else None
        cands.append((score, name, images, url, cf))
        log.info(f"  [cand] texto={score:.2f} img={'sí' if cf else '—'} '{name}' {url}")
    return cands


def _ddg_candidates(page, title: str, q_feats: list) -> list:
    """Fallback: candidatos por DDG (si el sitemap no dio catálogo)."""
    clean = _tokenize(_clean_for_match(title))
    title_sp = _species_of_text(title)
    cands = []
    for url in _ddg_find_product_urls(title):
        if not _species_ok(title_sp, url, ""):
            log.info(f"  [especie] descarta {url} (≠ {title_sp})")
            continue
        name, images = _try_url(page, url)
        if not name:
            continue
        if not _species_ok(title_sp, url, name):
            continue
        cf = _candidate_feat(page, images) if q_feats else None
        cands.append((_similarity(clean, _tokenize(name)), name, images, url, cf))
        log.info(f"  [cand-ddg] texto={cands[-1][0]:.2f} img={'sí' if cf else '—'} "
                 f"'{name}' {url}")
    return cands


def find_best_match(shopify_title: str, catalog: dict,
                    barcode: str = "", product_images: list = None) -> tuple:
    """
    Catálogo completo del sitemap (determinista) + filtro de imagen:
    1. Cache de resolución (many-to-one: tallas de la misma receta).
    2. Candidatos = top-K del catálogo rankeado por texto (EN↔ES). Si no hay
       catálogo (sitemap falló) → fallback DDG.
    3. GATE de imagen (si usar_imagen + CLIP): solo se aceptan candidatos cuya
       foto confirma la de Shopify (≥ gate_threshold); entre confirmados gana el
       de mejor texto. Ninguno confirma → sin_match (vacío, no inventa).
       Sin CLIP/sin foto → solo texto (umbral MATCH_THRESHOLD).
    Devuelve (handle, score); handle es clave en catalog con la URL real.
    """
    title_key = _title_key(shopify_title)
    if title_key in _RESOLVED:
        slug, sc = _RESOLVED[title_key]
        log.info(f"  Cache resolución: {title_key} → {slug or 'sin_match'}")
        return (title_key, sc) if slug else (None, sc)

    page = _get_page()
    if page is None:
        return None, 0.0

    q_feats = _shopify_feats(product_images)
    if _has_real_catalog(catalog):
        cands = _catalog_candidates(page, shopify_title, catalog, q_feats)
    else:
        log.info("  (sin catálogo sitemap — fallback DDG)")
        cands = _ddg_candidates(page, shopify_title, q_feats)

    if not cands:
        _RESOLVED[title_key] = (None, 0.0)
        log.warning(f"  Sin resolución para '{shopify_title}'")
        return None, 0.0

    gate_active = (image_match is not None and getattr(image_match, "ENABLED", False)
                   and bool(q_feats) and image_match.backend() == "clip")

    def _accept(score, name, images, url, why):
        catalog[title_key] = {"name": name, "url": url, "images": images}
        _RESOLVED[title_key] = (title_key, score)
        _save_catalog(catalog)
        log.info(f"  ✓ Resuelto ({why}): '{name}' (texto={score:.2f}) {url}")
        return title_key, score

    def _no_match(score):
        _RESOLVED[title_key] = (None, score)
        return None, score

    if gate_active:
        gate = image_match.gate_threshold()
        survivors = []
        for score, name, images, url, cf in cands:
            if cf is None:
                continue
            isim = image_match.best_similarity(q_feats, cf)
            if isim >= gate:
                survivors.append((score, name, images, url, isim))
            else:
                log.info(f"  [img-gate] descarta '{name}': img={isim:.2f} < {gate:.2f}")
        if not survivors:
            log.warning("  [img-gate] ningún candidato confirma imagen → sin_match (vacío)")
            return _no_match(0.0)
        score, name, images, url, isim = max(survivors, key=lambda c: c[0])
        if score < _TEXT_MIN_WITH_IMG:
            log.warning(f"  Confirmado por imagen (img={isim:.2f}) pero texto={score:.2f}"
                        f" < {_TEXT_MIN_WITH_IMG} → sin_match")
            return _no_match(score)
        return _accept(score, name, images, url, f"img-gate OK, img={isim:.2f}")

    if image_match is not None and getattr(image_match, "ENABLED", False) \
            and product_images and image_match.backend() != "clip":
        log.info("  [img-gate] CLIP no cargó → solo texto")
    score, name, images, url, _ = max(cands, key=lambda c: c[0])
    if score >= MATCH_THRESHOLD:
        return _accept(score, name, images, url, f"texto={score:.2f}")
    log.warning(f"  Mejor candidato score={score:.2f} < {MATCH_THRESHOLD} → sin_match")
    return _no_match(score)
