"""
Scraper para Artero — artero.com/es/petcare/
Usa Playwright porque el sitio bloquea requests directos (403).

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

CATALOG_PATH = Path("resultados/artero_catalog.json")
MIN_SCORE    = 0.10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

IGNORE_TOKENS = {
    "artero", "professional", "pet",
    "de", "el", "la", "los", "las", "con", "sin", "y", "para",
    "ml", "gr", "g", "kg", "l", "x", "oz", "ud", "uds",
    "a", "e", "o",
}


# ─── Extracción de imágenes ────────────────────────────────────────────────────

def _bump_resolution(url: str) -> str:
    """Intenta pedir la máxima resolución eliminando parámetros de tamaño pequeño."""
    url_clean = re.sub(r'-\d+x\d+\.', '.', url)  # elimina sufijos -300x300
    url_clean = url_clean.split("?")[0]            # quita parámetros de CDN
    return url_clean


def _extract_images(page, base_domain: str) -> list:
    """Extrae URLs de imágenes de producto de la página actual (múltiples estrategias)."""
    urls: set = set()

    # 1. <img> con src del mismo dominio (producto, no icono)
    try:
        for el in page.query_selector_all("img[src]"):
            src = el.get_attribute("src") or ""
            if not src or src.startswith("data:"):
                continue
            full = src if src.startswith("http") else (
                f"https:{src}" if src.startswith("//") else f"https://{base_domain}{src}"
            )
            # Sólo imágenes del dominio principal (no SVG ni iconos pequeños)
            if base_domain not in full:
                continue
            if any(kw in full.lower() for kw in (".svg", "logo", "icon", "banner",
                                                   "sprite", "arrow", "placeholder")):
                continue
            try:
                w = page.evaluate("el => el.naturalWidth", el) or 0
                if 0 < w < 100:
                    continue
            except Exception:
                pass
            urls.add(_bump_resolution(full))
    except Exception:
        pass

    # 2. srcset — tomar la URL de mayor resolución (último elemento)
    try:
        for el in page.query_selector_all("img[srcset]"):
            srcset = el.get_attribute("srcset") or ""
            parts  = [p.strip() for p in srcset.split(",") if p.strip()]
            if parts:
                candidate = parts[-1].split()[0]
                if candidate and not candidate.startswith("data:"):
                    full = candidate if candidate.startswith("http") else f"https:{candidate}"
                    if base_domain in full:
                        urls.add(_bump_resolution(full))
    except Exception:
        pass

    # 3. data-src / lazy loading
    for attr in ("data-src", "data-lazy-src", "data-original", "data-zoom-image",
                 "data-large-image", "data-full-url"):
        try:
            for el in page.query_selector_all(f"img[{attr}]"):
                src = el.get_attribute(attr) or ""
                if src and not src.startswith("data:"):
                    full = src if src.startswith("http") else f"https:{src}"
                    if base_domain in full:
                        urls.add(_bump_resolution(full))
        except Exception:
            pass

    # 4. JSON-LD (schema.org Product / ImageObject)
    try:
        ld_texts = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
                        .map(s => s.textContent || '');
        }""")
        for text in (ld_texts or []):
            try:
                data = json.loads(text)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    imgs = item.get("image", [])
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

    # 5. Variables JS con URLs de imágenes (WooCommerce gallery, etc.)
    try:
        js_texts = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('script:not([src])'))
                        .map(s => s.textContent || '')
                        .filter(t => t.includes('gallery') || t.includes('"image"')
                                  || t.includes('product_image') || t.includes('wc-product'));
        }""")
        for text in (js_texts or []):
            for m in re.finditer(
                r'https?://[^\s"\'<>\\]+\.(?:jpg|jpeg|png|webp)(?:[^\s"\'<>\\]*)?',
                text
            ):
                url = m.group(0).rstrip("\\,;}")
                if base_domain in url and len(url) < 500:
                    urls.add(_bump_resolution(url))
    except Exception:
        pass

    # Filtrar imágenes claramente no-producto
    filtered = {
        u for u in urls
        if not any(kw in u.lower() for kw in ("logo", "icon", "banner", "sprite",
                                               "arrow", "placeholder", "favicon",
                                               "thumbnail-placeholder"))
    }
    return list(filtered)


# ─── Paginación ───────────────────────────────────────────────────────────────

def _collect_product_links(page, base_url: str, base_domain: str) -> set:
    """
    Recorre la página de categoría (y las siguientes vía paginación) y
    devuelve todos los hrefs que parecen páginas de producto individual.
    """
    product_urls: set = set()
    visited_pages: set = set()
    current_url = base_url

    while current_url and current_url not in visited_pages:
        visited_pages.add(current_url)
        log.info(f"  Cargando categoría: {current_url}")
        try:
            page.goto(current_url, timeout=45000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(2000)
        except Exception as e:
            log.warning(f"  Error cargando {current_url}: {e}")
            break

        # Recoger enlaces de producto: más profundos que la URL base y con extensión o slug
        try:
            for el in page.query_selector_all("a[href]"):
                href = (el.get_attribute("href") or "").split("?")[0].rstrip("/")
                if not href:
                    continue
                if href.startswith("/"):
                    href = f"https://{base_domain}{href}"
                elif not href.startswith("http"):
                    continue
                if base_domain not in href:
                    continue
                # El enlace debe ser más profundo que la URL base (más segmentos)
                base_segments = [s for s in base_url.rstrip("/").split("/") if s]
                href_segments = [s for s in href.split("/") if s]
                if len(href_segments) <= len(base_segments):
                    continue
                # Excluir páginas de navegación, carrito, etc.
                if any(kw in href.lower() for kw in ("#", "javascript:", "mailto:",
                                                       "cart", "checkout", "account",
                                                       "login", "register", "contact",
                                                       "about", "blog", "news",
                                                       "privacy", "legal", "terms",
                                                       "search", "wishlist", "compare")):
                    continue
                product_urls.add(href)
        except Exception as e:
            log.debug(f"  Error recogiendo enlaces: {e}")

        # Paginación: buscar botón/enlace "siguiente"
        next_url = None
        try:
            for selector in (
                "a.next", "a[rel='next']", ".pagination a.next",
                "a:has-text('Siguiente')", "a:has-text('>')",
                "a:has-text('→')", ".woocommerce-pagination a.next",
                "li.next a", ".nav-next a",
            ):
                try:
                    next_el = page.query_selector(selector)
                    if next_el:
                        href = (next_el.get_attribute("href") or "").strip()
                        if href and href not in visited_pages:
                            if href.startswith("/"):
                                href = f"https://{base_domain}{href}"
                            next_url = href
                            break
                except Exception:
                    continue
        except Exception:
            pass

        current_url = next_url

    log.info(f"  Total enlaces recogidos: {len(product_urls)}")
    return product_urls


# ─── Interfaz pública: scrape_catalog ─────────────────────────────────────────

def scrape_catalog(web_url: str, rebuild: bool = False) -> dict:
    """
    Scrapea artero.com con Playwright y construye el catálogo de productos.
    Devuelve: { handle: { name, url, images: [url, ...] } }
    Cachea en resultados/artero_catalog.json.
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

    base_domain = web_url.split("/")[2] if web_url.startswith("http") else "artero.com"
    catalog: dict = {}

    log.info(f"Scraping catálogo Artero con Playwright desde {web_url}...")

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

        # Recoger URLs de productos desde la página de categoría (con paginación)
        product_urls = _collect_product_links(page, web_url, base_domain)

        log.info(f"Procesando {len(product_urls)} páginas de producto...")
        for prod_url in sorted(product_urls):
            handle = prod_url.rstrip("/").split("/")[-1]
            if handle in catalog:
                continue

            try:
                page.goto(prod_url, timeout=45000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1500)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(500)

                name_el = (
                    page.query_selector("h1.product_title")
                    or page.query_selector("h1.product-title")
                    or page.query_selector("h1.entry-title")
                    or page.query_selector("h1")
                )
                name   = name_el.inner_text().strip() if name_el else handle
                images = _extract_images(page, base_domain)

                catalog[handle] = {
                    "name":   name,
                    "url":    prod_url,
                    "images": images,
                }
                log.info(f"  {handle}: {len(images)} imagen(es) — {name[:60]}")
                time.sleep(0.8)

            except Exception as e:
                log.warning(f"  Error en {prod_url}: {e}")

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
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return text


def _tokenize(text: str) -> set:
    tokens = set(_normalize(text).split())
    tokens -= IGNORE_TOKENS
    tokens -= {t for t in tokens if len(t) <= 1}
    return tokens


def find_best_match(shopify_title: str, catalog: dict) -> tuple:
    """
    Jaccard entre tokens del título Shopify y tokens handle+nombre del catálogo.
    Devuelve (handle, score). Si score < MIN_SCORE → (None, score).
    """
    title_tokens = _tokenize(shopify_title)
    best_handle, best_score = None, 0.0

    for handle, entry in catalog.items():
        cat_tokens = (
            _tokenize(handle.replace("-", " "))
            | _tokenize(entry.get("name", ""))
        )
        union = title_tokens | cat_tokens
        if not union:
            continue
        score = len(title_tokens & cat_tokens) / len(union)
        if score > best_score:
            best_score = score
            best_handle = handle

    if best_score < MIN_SCORE:
        return None, best_score
    return best_handle, best_score
