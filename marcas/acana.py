"""
Scraper para emea.acana.com — Demandware/Salesforce Commerce Cloud.
Requiere Playwright por la protección Cloudflare WAF.

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

CATALOG_PATH = Path("resultados/acana_catalog.json")
BASE_URL     = "https://emea.acana.com"
IMAGE_WIDTH  = 2000   # parámetro ?sw= para máxima resolución en CDN Demandware

CATEGORY_URLS = [
    f"{BASE_URL}/es-ES/para-gatos",
    f"{BASE_URL}/es-ES/para-perros",
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
    "acana", "eu", "aca", "new", "emea", "apac",
    "cat", "dog", "feline", "canine",
    "para", "de", "el", "la", "los", "las", "con", "sin",
    "kg", "gr", "g", "lb", "x",
    "adult", "adulto", "adultos",
}


# ─── Utilidades internas ──────────────────────────────────────────────────────

def _normalize_img_url(url: str) -> str:
    """Solicita la máxima resolución disponible en el CDN Demandware."""
    return f"{url.split('?')[0]}?sw={IMAGE_WIDTH}&q=100"


def _extract_image_urls(page) -> list:
    """Extrae URLs de imágenes de producto de la página actual."""
    urls: set = set()

    # 1. Atributos src / data-src / lazy-loading en <img> con URL de emea.acana.com
    for attr in ("src", "data-src", "data-lazy-src", "data-zoom-image"):
        try:
            for el in page.query_selector_all(f"img[{attr}*='emea.acana.com']"):
                src = el.get_attribute(attr) or ""
                if src and "demandware" in src:
                    urls.add(_normalize_img_url(src))
        except Exception:
            pass

    # 2. srcset (imágenes responsivas) — filtramos en Python para evitar
    #    selectores CSS complejos que pueden fallar en algunos entornos
    try:
        for el in page.query_selector_all("img[srcset]"):
            srcset = el.get_attribute("srcset") or ""
            for part in srcset.split(","):
                src = part.strip().split()[0] if part.strip() else ""
                if "emea.acana.com" in src and "demandware" in src:
                    urls.add(_normalize_img_url(src))
    except Exception:
        pass

    # 3. background-image en estilos inline
    try:
        raw_styles = page.evaluate("""() => {
            var els = document.querySelectorAll('[style]');
            var result = [];
            for (var i = 0; i < els.length; i++) {
                var s = els[i].getAttribute('style') || '';
                if (s.indexOf('background-image') !== -1 && s.indexOf('emea.acana.com') !== -1) {
                    result.push(s);
                }
            }
            return result;
        }""")
        for style in (raw_styles or []):
            m = re.search(r'url\(["\']?(https://emea\.acana\.com[^"\')\s]+)["\']?\)', style)
            if m:
                urls.add(_normalize_img_url(m.group(1)))
    except Exception:
        pass

    # 4. JSON-LD (schema.org Product) — buscar URLs en texto crudo de los scripts
    try:
        ld_texts = page.evaluate("""() => {
            var scripts = document.querySelectorAll('script');
            var result = [];
            for (var i = 0; i < scripts.length; i++) {
                var t = scripts[i].getAttribute('type') || '';
                if (t === 'application/ld+json') {
                    result.push(scripts[i].textContent || '');
                }
            }
            return result;
        }""")
        for text in (ld_texts or []):
            for img_url in re.findall(r'https://emea\.acana\.com/dw/image[^"\'<>\s\\]+', text):
                urls.add(_normalize_img_url(img_url))
    except Exception:
        pass

    # 5. Variables JS embebidas con URLs de imágenes Demandware
    try:
        js_texts = page.evaluate("""() => {
            var scripts = document.querySelectorAll('script');
            var result = [];
            for (var i = 0; i < scripts.length; i++) {
                if (scripts[i].src) continue;
                var t = scripts[i].textContent || '';
                if (t.indexOf('dw/image') !== -1) {
                    result.push(t);
                }
            }
            return result;
        }""")
        for text in (js_texts or []):
            for img_url in re.findall(r'https://emea\.acana\.com/dw/image[^"\'<>\s\\]+', text):
                urls.add(_normalize_img_url(img_url))
    except Exception:
        pass

    return list(urls)


# ─── Interfaz pública ─────────────────────────────────────────────────────────

def scrape_catalog(web_url: str, rebuild: bool = False) -> dict:
    """
    Scrapea emea.acana.com con Playwright y construye el catálogo.
    Devuelve: { handle: { name, url, category, images: [url, ...] } }
    Cachea el resultado en resultados/acana_catalog.json.
    """
    if not rebuild and CATALOG_PATH.exists():
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        log.info(f"Catálogo cargado desde caché: {len(catalog)} productos")
        return catalog

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright no instalado: pip install playwright && "
                  "playwright install chromium")
        return {}

    catalog: dict = {}
    log.info("Scraping catálogo Acana con Playwright...")

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
        )
        page = ctx.new_page()

        for cat_url in CATEGORY_URLS:
            category    = "cat" if "gatos" in cat_url else "dog"
            cat_segment = "para-gatos" if category == "cat" else "para-perros"

            log.info(f"  Cargando: {cat_url}")
            try:
                page.goto(cat_url, timeout=45000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(2000)
            except Exception as e:
                log.warning(f"  Error cargando {cat_url}: {e}")
                continue

            # Recoger enlaces de producto — filtrar en Python por cat_segment
            product_urls: set = set()
            try:
                for link in page.query_selector_all("a[href]"):
                    href = link.get_attribute("href") or ""
                    if cat_segment in href and href.endswith(".html"):
                        full = href if href.startswith("http") else BASE_URL + href
                        product_urls.add(full.split("?")[0])
            except Exception as e:
                log.warning(f"  Error recogiendo enlaces: {e}")

            log.info(f"  {len(product_urls)} productos en {category}")

            for prod_url in sorted(product_urls):
                handle = prod_url.rstrip("/").split("/")[-1].replace(".html", "")
                try:
                    page.goto(prod_url, timeout=45000, wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1500)
                    page.evaluate("window.scrollTo(0, 0)")
                    page.wait_for_timeout(500)

                    name_el = (
                        page.query_selector("h1.product-name")
                        or page.query_selector("h1")
                        or page.query_selector(".product-name")
                    )
                    name   = name_el.inner_text().strip() if name_el else handle
                    images = _extract_image_urls(page)

                    catalog[handle] = {
                        "name": name,
                        "url": prod_url,
                        "category": category,
                        "images": images,
                    }
                    log.info(f"    {handle}: {len(images)} imagen(es)")
                    time.sleep(1)
                except Exception as e:
                    log.warning(f"    Error en {prod_url}: {e}")

        browser.close()

    CATALOG_PATH.parent.mkdir(exist_ok=True)
    CATALOG_PATH.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(f"Catálogo guardado: {len(catalog)} productos → {CATALOG_PATH}")
    return catalog


# ─── Matching ─────────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9\s]", " ", text)


def _tokenize(text: str) -> set:
    tokens = set(_normalize(text).split())
    return tokens - IGNORE_TOKENS - {t for t in tokens if len(t) <= 1}


def find_best_match(shopify_title: str, catalog: dict) -> tuple:
    """Jaccard entre tokens del título Shopify y tokens handle+nombre del catálogo."""
    title_tokens = _tokenize(shopify_title)
    best_handle, best_score = None, 0.0
    for handle, entry in catalog.items():
        cat_tokens = (
            _tokenize(handle.replace("-", " ")) | _tokenize(entry.get("name", ""))
        )
        union = title_tokens | cat_tokens
        score = len(title_tokens & cat_tokens) / len(union) if union else 0.0
        if score > best_score:
            best_score = score
            best_handle = handle
    return best_handle, best_score
