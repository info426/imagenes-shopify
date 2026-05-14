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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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
    return f"{url.split('?')[0]}?sw={IMAGE_WIDTH}"


def _extract_image_urls(page) -> list:
    urls = set()

    for attr in ("src", "data-src", "data-lazy-src"):
        for el in page.query_selector_all(f"img[{attr}*='emea.acana.com']"):
            src = el.get_attribute(attr) or ""
            if src and "demandware" in src:
                urls.add(_normalize_img_url(src))

    raw_styles = page.evaluate("""() => {
        const els = document.querySelectorAll('[style*="background-image"]');
        return Array.from(els)
            .map(e => e.getAttribute('style'))
            .filter(s => s && s.includes('emea.acana.com'));
    }""")
    for style in raw_styles:
        m = re.search(r'url\(["\'\]?(https://[^"\'\'\)\s]+)["\'\'\]?\)', style)
        if m:
            urls.add(_normalize_img_url(m.group(1)))

    try:
        json_data = page.evaluate("""() => {
            const el = document.querySelector('[data-product-images]') ||
                       document.querySelector('.product-images');
            return el ? el.getAttribute('data-product-images') || el.textContent : null;
        }""")
        if json_data:
            for url in re.findall(r'https://emea\.acana\.com/dw/image[^"\'\\ s]+',
                                  json_data):
                urls.add(_normalize_img_url(url))
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

    catalog = {}
    log.info("Scraping catálogo Acana con Playwright...")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="es-ES",
            extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]},
        )
        page = ctx.new_page()

        for cat_url in CATEGORY_URLS:
            category = "cat" if "gatos" in cat_url else "dog"
            log.info(f"  Cargando: {cat_url}")
            try:
                page.goto(cat_url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(2000)

                cat_segment = "para-gatos" if category == "cat" else "para-perros"
                product_urls = set()
                for link in page.query_selector_all('a[href$=".html"]'):
                    href = link.get_attribute("href") or ""
                    if href and cat_segment in href:
                        product_urls.add(
                            href if href.startswith("http") else BASE_URL + href
                        )

                log.info(f"  {len(product_urls)} productos en {category}")

                for prod_url in sorted(product_urls):
                    handle = prod_url.rstrip("/").split("/")[-1].replace(".html", "")
                    try:
                        page.goto(prod_url, timeout=30000,
                                  wait_until="domcontentloaded")
                        page.wait_for_timeout(2000)
                        name_el = (page.query_selector("h1.product-name") or
                                   page.query_selector("h1") or
                                   page.query_selector(".product-name"))
                        name   = name_el.inner_text().strip() if name_el else handle
                        images = _extract_image_urls(page)
                        catalog[handle] = {
                            "name": name, "url": prod_url,
                            "category": category, "images": images,
                        }
                        log.info(f"    {handle}: {len(images)} imagen(es)")
                        time.sleep(1)
                    except Exception as e:
                        log.warning(f"    Error en {prod_url}: {e}")

            except Exception as e:
                log.warning(f"  Error cargando {cat_url}: {e}")

        browser.close()

    CATALOG_PATH.parent.mkdir(exist_ok=True)
    CATALOG_PATH.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(f"Catálogo guardado: {len(catalog)} productos → {CATALOG_PATH}")
    return catalog


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
        cat_tokens = (_tokenize(handle.replace("-", " ")) |
                      _tokenize(entry.get("name", "")))
        union = title_tokens | cat_tokens
        score = len(title_tokens & cat_tokens) / len(union) if union else 0.0
        if score > best_score:
            best_score = score
            best_handle = handle
    return best_handle, best_score
