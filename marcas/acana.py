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
    base = url.split("?")[0]
    return f"{base}?sw={IMAGE_WIDTH}&q=100"


def _is_product_img(url: str) -> bool:
    return bool(url and (
        "emea.acana.com/dw/image" in url
        or "demandware.net" in url
        or (
            "emea.acana.com" in url
            and any(ext in url.lower() for ext in (".jpg", ".jpeg", ".png", ".webp"))
        )
    ))


def _extract_image_urls(page) -> list:
    """Extrae todas las URLs de imágenes de producto de la página actual."""
    urls: set[str] = set()

    # 1. src / data-src / lazy-loading y atributos de zoom
    for attr in ("src", "data-src", "data-lazy-src", "data-zoom-image",
                 "data-full", "data-image-url", "data-zoom-src"):
        for el in page.query_selector_all(f"img[{attr}]"):
            src = el.get_attribute(attr) or ""
            if _is_product_img(src):
                urls.add(_normalize_img_url(src))
        # también en elementos no-img que contengan la URL
        for el in page.query_selector_all(f"[{attr}*='acana.com']"):
            src = el.get_attribute(attr) or ""
            if _is_product_img(src):
                urls.add(_normalize_img_url(src))

    # 2. srcset (imágenes responsivas — quedarse con la URL sin descriptor)
    for el in page.query_selector_all("img[srcset]"):
        srcset = el.get_attribute("srcset") or ""
        for part in srcset.split(","):
            src = part.strip().split()[0] if part.strip() else ""
            if _is_product_img(src):
                urls.add(_normalize_img_url(src))

    # 3. background-image en estilos inline
    raw_styles: list = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('[style*="background-image"]'))
            .map(e => e.getAttribute('style') || '')
            .filter(s => s.includes('acana.com') || s.includes('demandware'));
    }""")
    for style in raw_styles:
        m = re.search(r'url\(["\']?(https://[^"\')\s]+)["\']?\)', style)
        if m and _is_product_img(m.group(1)):
            urls.add(_normalize_img_url(m.group(1)))

    # 4. JSON-LD (schema.org Product)
    try:
        scripts: list = page.evaluate("""() =>
            Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
                .map(s => s.textContent || '')
        """)
        for script in scripts:
            if not script:
                continue
            try:
                data = json.loads(script)
                images = data.get("image", []) if isinstance(data, dict) else []
                if isinstance(images, str):
                    images = [images]
                for img_url in images:
                    if _is_product_img(img_url):
                        urls.add(_normalize_img_url(img_url))
            except (json.JSONDecodeError, Exception):
                pass
            # Fallback: extraer URLs de imagen del texto crudo
            for img_url in re.findall(
                r'https://[^\s"\'\\<>]+(?:dw/image)[^\s"\'\\<>]*', script
            ):
                urls.add(_normalize_img_url(img_url))
    except Exception:
        pass

    # 5. Variables JS embebidas (SFCC product data en scripts inline)
    try:
        js_text: str = page.evaluate("""() => {
            const out = [];
            for (const s of document.querySelectorAll('script:not([src])')) {
                const t = s.textContent || '';
                if (t.includes('dw/image') || t.includes('demandware.net')) {
                    out.push(t);
                }
            }
            return out.join('\\n');
        }""")
        for img_url in re.findall(
            r'https://[^\s"\'\\<>]+(?:dw/image|demandware\.net)[^\s"\'\\<>]*',
            js_text,
        ):
            if _is_product_img(img_url):
                urls.add(_normalize_img_url(img_url))
    except Exception:
        pass

    return list(urls)


def _collect_product_urls(page, cat_url: str, cat_segment: str) -> set:
    """Carga la categoría activando scroll/paginación y recoge las URLs de producto."""
    product_urls: set[str] = set()

    log.info(f"  Cargando: {cat_url}")
    try:
        page.goto(cat_url, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
    except Exception as e:
        log.warning(f"  Error cargando {cat_url}: {e}")
        return product_urls

    # Scroll progresivo + clic en "cargar más" hasta agotar productos
    prev_count = 0
    for attempt in range(8):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)

        clicked = False
        for selector in (
            "button.load-more", ".btn-load-more", "[data-action='more']",
            "a.show-more", ".show-more-button",
            "button:has-text('Ver más')", "button:has-text('Load more')",
            "button:has-text('Mostrar más')",
        ):
            try:
                btn = page.query_selector(selector)
                if btn and btn.is_visible():
                    btn.click()
                    page.wait_for_timeout(3000)
                    log.info(f"    Cargados más productos (intento {attempt + 1})")
                    clicked = True
                    break
            except Exception:
                pass

        # Contar productos actuales para detectar si ya no hay más
        current_links = page.query_selector_all(f"a[href*='{cat_segment}'][href$='.html']")
        if len(current_links) == prev_count and not clicked:
            break
        prev_count = len(current_links)

    for link in page.query_selector_all("a[href]"):
        href = link.get_attribute("href") or ""
        if href and cat_segment in href and href.endswith(".html"):
            full = href if href.startswith("http") else BASE_URL + href
            product_urls.add(full.split("?")[0])

    log.info(f"  → {len(product_urls)} productos en /{cat_segment}")
    return product_urls


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
            category   = "cat" if "gatos" in cat_url else "dog"
            cat_segment = "para-gatos" if category == "cat" else "para-perros"
            product_urls = _collect_product_urls(page, cat_url, cat_segment)

            for prod_url in sorted(product_urls):
                handle = prod_url.rstrip("/").split("/")[-1].replace(".html", "")
                try:
                    page.goto(prod_url, timeout=45000, wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)

                    # Scroll completo para activar lazy-load de imágenes
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1500)
                    page.evaluate("window.scrollTo(0, 0)")
                    page.wait_for_timeout(1000)

                    name_el = (
                        page.query_selector("h1.product-name")
                        or page.query_selector("[itemprop='name']")
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
                    time.sleep(1.5)
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
