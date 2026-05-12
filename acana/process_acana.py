#!/usr/bin/env python3
"""
Procesador de imágenes Acana para Shopify
==========================================
Scrapea el catálogo de emea.acana.com con Playwright (necesario por Cloudflare),
descarga las imágenes oficiales, las convierte a WebP 2000×2000 y las sube a Shopify.

Uso:
  python3 acana/process_acana.py                         # todos los productos Acana
  python3 acana/process_acana.py --product-id 123456     # modo prueba
  python3 acana/process_acana.py --only-ids 123,456      # lista específica
  python3 acana/process_acana.py --rebuild-catalog       # re-scrapea la web
  PRODUCT_IDS=123,456 python3 acana/process_acana.py
"""

import os
import re
import sys
import time
import json
import base64
import unicodedata
import logging
import argparse
import requests
from pathlib import Path
from io import BytesIO
from PIL import Image, ImageFilter
from dotenv import load_dotenv

load_dotenv()

SHOP_DOMAIN    = os.getenv("SHOP_DOMAIN",   "7ev1zx-eg.myshopify.com")
CLIENT_ID      = os.getenv("CLIENT_ID",     "")
CLIENT_SECRET  = os.getenv("CLIENT_SECRET", "")
VENDOR         = "Acana"
API_VERSION    = "2024-10"

TARGET_SIZE    = (2000, 2000)
WEBP_QUALITY   = 90
PADDING        = 0.05
WHITE_THRESH   = 245
WHITE_MIN_FRAC = 0.60
MIN_DIM        = 800    # mínimo px en cualquier dimensión para aceptar una imagen

RESULTS_DIR    = Path("resultados")
CATALOG_PATH   = RESULTS_DIR / "acana_catalog.json"
ORIGINALS_DIR  = RESULTS_DIR / "originals_acana"

BASE_URL       = "https://emea.acana.com"
CATEGORY_URLS  = [
    f"{BASE_URL}/es-ES/para-gatos",
    f"{BASE_URL}/es-ES/para-perros",
]
IMAGE_WIDTH    = 2000

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

IGNORE_TOKENS = {
    "acana", "eu", "aca", "new", "emea", "apac",
    "cat", "dog", "feline", "canine",
    "para", "de", "el", "la", "los", "las", "con", "sin",
    "kg", "gr", "g", "lb", "x",
    "adult", "adulto", "adultos",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("acana_process.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def get_token() -> str:
    resp = requests.post(
        f"https://{SHOP_DOMAIN}/admin/oauth/access_token",
        data={"grant_type": "client_credentials",
              "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise ValueError(f"No se pudo obtener token: {resp.text}")
    log.info("Token obtenido")
    return token


class ShopifyAPI:
    def __init__(self, token: str):
        self.base = f"https://{SHOP_DOMAIN}/admin/api/{API_VERSION}"
        self.h = {"X-Shopify-Access-Token": token,
                  "Content-Type": "application/json"}

    def get_products(self, vendor: str) -> list:
        products, params = [], {"limit": 250, "vendor": vendor}
        url = f"{self.base}/products.json"
        while url:
            r = requests.get(url, headers=self.h, params=params, timeout=30)
            r.raise_for_status()
            products.extend(r.json().get("products", []))
            params, url = {}, None
            link = r.headers.get("Link", "")
            if 'rel="next"' in link:
                for part in link.split(","):
                    if 'rel="next"' in part:
                        url = part.strip().split(";")[0].strip("<>")
        return products

    def get_product(self, pid: int) -> dict:
        r = requests.get(f"{self.base}/products/{pid}.json",
                         headers=self.h, timeout=30)
        r.raise_for_status()
        return r.json()["product"]

    def get_images(self, pid: int) -> list:
        r = requests.get(f"{self.base}/products/{pid}/images.json",
                         headers=self.h, timeout=30)
        r.raise_for_status()
        return r.json().get("images", [])

    def delete_image(self, pid: int, img_id: int):
        requests.delete(
            f"{self.base}/products/{pid}/images/{img_id}.json",
            headers=self.h, timeout=30,
        ).raise_for_status()

    def upload_image(self, pid: int, b64: str, filename: str,
                     alt: str = "", position: int = None) -> dict:
        payload = {"attachment": b64, "filename": filename, "alt": alt}
        if position is not None:
            payload["position"] = position
        r = requests.post(
            f"{self.base}/products/{pid}/images.json",
            headers=self.h, json={"image": payload}, timeout=60,
        )
        r.raise_for_status()
        return r.json()


def _extract_image_urls_from_page(page) -> list:
    urls = set()
    for attr in ("src", "data-src", "data-lazy-src"):
        elements = page.query_selector_all(f"img[{attr}*='emea.acana.com']")
        for el in elements:
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
        match = re.search(r'url\(["\']?(https://[^"\')\s]+)["\']?\)', style)
        if match:
            urls.add(_normalize_img_url(match.group(1)))
    try:
        json_data = page.evaluate("""() => {
            const el = document.querySelector('[data-product-images]') ||
                       document.querySelector('.product-images');
            return el ? el.getAttribute('data-product-images') || el.textContent : null;
        }""")
        if json_data:
            for url in re.findall(r'https://emea\.acana\.com/dw/image[^"\'\\ s]+', json_data):
                urls.add(_normalize_img_url(url))
    except Exception:
        pass
    return list(urls)


def _normalize_img_url(url: str) -> str:
    return f"{url.split('?')[0]}?sw={IMAGE_WIDTH}"


def scrape_catalog(rebuild: bool = False) -> dict:
    if not rebuild and CATALOG_PATH.exists():
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        log.info(f"Catálogo cargado desde caché: {len(catalog)} productos")
        return catalog
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright no instalado. Ejecuta: pip install playwright && playwright install chromium")
        sys.exit(1)
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
            log.info(f"  Cargando categoría: {cat_url}")
            try:
                page.goto(cat_url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(2000)
                links = page.query_selector_all('a[href$=".html"]')
                product_urls = set()
                cat_segment = "para-gatos" if category == "cat" else "para-perros"
                for link in links:
                    href = link.get_attribute("href") or ""
                    if href and cat_segment in href:
                        full_url = href if href.startswith("http") else BASE_URL + href
                        product_urls.add(full_url)
                log.info(f"  {len(product_urls)} productos encontrados en {category}")
                for prod_url in sorted(product_urls):
                    handle = prod_url.rstrip("/").split("/")[-1].replace(".html", "")
                    try:
                        page.goto(prod_url, timeout=30000, wait_until="domcontentloaded")
                        page.wait_for_timeout(2000)
                        name_el = (page.query_selector("h1.product-name") or
                                   page.query_selector("h1") or
                                   page.query_selector(".product-name"))
                        name = name_el.inner_text().strip() if name_el else handle
                        images = _extract_image_urls_from_page(page)
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
            except Exception as e:
                log.warning(f"  Error cargando {cat_url}: {e}")
        browser.close()
    RESULTS_DIR.mkdir(exist_ok=True)
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


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_best_match(shopify_title: str, catalog: dict) -> tuple:
    title_tokens = _tokenize(shopify_title)
    best_handle, best_score = None, 0.0
    for handle, entry in catalog.items():
        catalog_tokens = _tokenize(handle.replace("-", " ")) | _tokenize(entry.get("name", ""))
        score = _jaccard(title_tokens, catalog_tokens)
        if score > best_score:
            best_score = score
            best_handle = handle
    return best_handle, best_score


def download_raw(url: str) -> tuple:
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
    ext = "jpg" if ext not in ("jpg", "jpeg", "png", "webp") or ext == "jpeg" else ext
    return r.content, ext


def _is_high_res(raw: bytes) -> tuple:
    """Devuelve (ok, width, height). ok=True si ambas dimensiones >= MIN_DIM."""
    try:
        img = Image.open(BytesIO(raw))
        w, h = img.size
        return (w >= MIN_DIM and h >= MIN_DIM), w, h
    except Exception:
        return False, 0, 0


def search_web_images(product_name: str, exclude_domain: str = "emea.acana.com",
                      max_results: int = 5) -> list:
    """
    Busca en DuckDuckGo imágenes de alta resolución del producto en fuentes
    distintas a exclude_domain. Devuelve lista de (bytes, ext).
    """
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        log.warning("  [DDG] duckduckgo-search no instalado — omitiendo búsqueda web")
        return []

    query = f"Acana {product_name} product"
    log.info(f"  [DDG] Buscando: «{query}»")
    found = []
    try:
        with DDGS() as ddgs:
            hits = list(ddgs.images(query, max_results=max_results * 4, size="Large"))
        for r in hits:
            img_url = r.get("image", "")
            if not img_url or exclude_domain in img_url:
                continue
            try:
                raw, ext = download_raw(img_url)
                ok, w, h = _is_high_res(raw)
                domain = img_url.split("/")[2] if img_url.startswith("http") else "?"
                if ok:
                    log.info(f"  [DDG] ✓ {domain}  {w}×{h}")
                    found.append((raw, ext))
                    if len(found) >= max_results:
                        break
                else:
                    log.info(f"  [DDG] baja res {w}×{h} — omitida ({domain})")
            except Exception as e:
                log.debug(f"  [DDG] Error descargando {img_url}: {e}")
            time.sleep(0.5)
    except Exception as e:
        log.warning(f"  [DDG] Error en búsqueda: {e}")
    return found


def _composite_on_white(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba.convert("RGB"), mask=rgba.split()[3])
        return bg
    return img.convert("RGB")


def _fill_transparent_with_blur(img_rgba: Image.Image) -> Image.Image:
    alpha = img_rgba.split()[3]
    rgb = img_rgba.convert("RGB")
    blurred = rgb.filter(ImageFilter.GaussianBlur(radius=40))
    result = blurred.copy()
    result.paste(rgb, (0, 0), mask=alpha)
    return result


def _is_white_background(img_rgb: Image.Image) -> bool:
    w, h = img_rgb.size
    patch = max(20, int(min(w, h) * 0.05))
    step = 2
    total = white = 0
    for x1, y1, x2, y2 in [
        (0, 0, patch, patch), (w - patch, 0, w, patch),
        (0, h - patch, patch, h), (w - patch, h - patch, w, h),
    ]:
        for x in range(x1, x2, step):
            for y in range(y1, y2, step):
                r, g, b = img_rgb.getpixel((x, y))
                white += int(r >= WHITE_THRESH and g >= WHITE_THRESH and b >= WHITE_THRESH)
                total += 1
    ratio = white / total if total > 0 else 0.0
    log.info(f"    [bg check] {white}/{total} ({ratio:.0%})")
    return ratio >= WHITE_MIN_FRAC


def process_image(img: Image.Image) -> Image.Image:
    has_alpha = (img.mode in ("RGBA", "LA") or
                 (img.mode == "P" and "transparency" in img.info))
    if has_alpha:
        rgba = img.convert("RGBA")
        alpha = rgba.split()[3]
        hist = alpha.histogram()
        transparent_ratio = sum(hist[:128]) / (img.width * img.height)
        log.info(f"    [alpha check] {transparent_ratio:.0%} transparente")
        if transparent_ratio > 0.15:
            # Producto sobre fondo transparente → componer sobre blanco
            composited = _composite_on_white(rgba)
        else:
            # Imagen mayormente opaca con esquinas transparentes → blur-fill
            composited = _fill_transparent_with_blur(rgba)
    else:
        composited = _composite_on_white(img)
    use_padding = _is_white_background(composited)
    log.info(f"    [{'padding 5%' if use_padding else 'sin padding'}]")
    max_w = int(TARGET_SIZE[0] * (1 - 2 * PADDING)) if use_padding else TARGET_SIZE[0]
    max_h = int(TARGET_SIZE[1] * (1 - 2 * PADDING)) if use_padding else TARGET_SIZE[1]
    ratio = composited.width / composited.height
    new_w = max_w if ratio >= 1 else int(max_h * ratio)
    new_h = int(max_w / ratio) if ratio >= 1 else max_h
    resized = composited.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGB", TARGET_SIZE, (255, 255, 255))
    canvas.paste(resized, ((TARGET_SIZE[0] - new_w) // 2,
                           (TARGET_SIZE[1] - new_h) // 2))
    return canvas


def to_webp_b64(img: Image.Image) -> str:
    buf = BytesIO()
    img.save(buf, format="WEBP", quality=WEBP_QUALITY, method=6)
    return base64.b64encode(buf.getvalue()).decode()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-id",      type=int, default=None)
    parser.add_argument("--only-ids",        type=str, default=None)
    parser.add_argument("--rebuild-catalog", action="store_true")
    args = parser.parse_args()

    if not CLIENT_ID or not CLIENT_SECRET:
        log.error("Faltan CLIENT_ID / CLIENT_SECRET")
        sys.exit(1)

    RESULTS_DIR.mkdir(exist_ok=True)
    ORIGINALS_DIR.mkdir(parents=True, exist_ok=True)

    log.info("=" * 60)
    log.info("PROCESADOR ACANA — SHOPIFY")
    log.info("=" * 60)

    catalog = scrape_catalog(rebuild=args.rebuild_catalog)
    if not catalog:
        log.error("Catálogo vacío — revisa el scraping")
        sys.exit(1)

    token = get_token()
    api   = ShopifyAPI(token)

    only_ids_raw = args.only_ids or os.getenv("PRODUCT_IDS", "")
    only_ids: set = set()
    if only_ids_raw:
        only_ids = {int(x.strip()) for x in only_ids_raw.split(",") if x.strip()}

    if args.product_id:
        products = [api.get_product(args.product_id)]
    elif only_ids:
        products = [api.get_product(pid) for pid in only_ids]
    else:
        log.info(f"Cargando productos Acana de Shopify...")
        products = api.get_products(VENDOR)
        log.info(f"Total: {len(products)} productos")

    if not products:
        log.warning("Sin productos.")
        return

    stats = dict(total=len(products), ok=0, sin_match=0, sin_imagen=0, errores=0)

    for i, product in enumerate(products, 1):
        pid   = product["id"]
        title = product["title"]
        log.info(f"\n[{i}/{len(products)}] {title}  (ID: {pid})")

        handle, score = find_best_match(title, catalog)
        if handle is None or score < 0.10:
            log.warning(f"  Sin match (score={score:.2f}) — saltando")
            stats["sin_match"] += 1
            continue

        entry = catalog[handle]
        log.info(f"  Match: {handle}  (score={score:.2f}, {len(entry['images'])} imgs)")

        if not entry["images"]:
            log.warning("  Sin imágenes en catálogo — saltando")
            stats["sin_imagen"] += 1
            continue

        raw_images = []
        for img_url in entry["images"]:
            try:
                raw, ext = download_raw(img_url)
                ok, w, h = _is_high_res(raw)
                fname = img_url.split('/')[-1].split('?')[0]
                if ok:
                    raw_images.append((raw, ext))
                    log.info(f"  Descargada: {fname}  {w}×{h}")
                else:
                    log.warning(f"  Baja resolución {w}×{h} — omitida: {fname}")
            except Exception as e:
                log.warning(f"  Error descargando {img_url}: {e}")

        if not raw_images:
            log.warning("  Sin imágenes de alta resolución en web oficial — buscando en internet...")
            raw_images = search_web_images(title)

        if not raw_images:
            log.warning("  No se encontraron imágenes de alta resolución — saltando")
            stats["sin_imagen"] += 1
            continue

        folder = ORIGINALS_DIR / str(pid)
        folder.mkdir(exist_ok=True)
        for j, (raw, ext) in enumerate(raw_images, 1):
            (folder / f"img_{j:02d}.{ext}").write_bytes(raw)

        processed = []
        for j, (raw, _) in enumerate(raw_images, 1):
            try:
                img = Image.open(BytesIO(raw))
                log.info(f"  Imagen {j}: {img.mode} {img.size}")
                processed.append(process_image(img))
            except Exception as e:
                log.warning(f"  Error procesando imagen {j}: {e}")

        if not processed:
            log.warning("  Ninguna imagen procesada — saltando")
            stats["errores"] += 1
            continue

        existing = api.get_images(pid)
        for img_data in existing:
            api.delete_image(pid, img_data["id"])
            time.sleep(0.2)
        log.info(f"  {len(existing)} imagen(es) antigua(s) eliminada(s)")

        for pos, img in enumerate(processed, 1):
            fname = f"acana_{pid}_{pos}.webp"
            api.upload_image(pid, to_webp_b64(img), fname, alt=title, position=pos)
            log.info(f"  ✓ Imagen {pos}/{len(processed)} subida")
            time.sleep(0.5)

        stats["ok"] += 1
        time.sleep(1)

    log.info("\n" + "=" * 60)
    log.info("RESUMEN")
    log.info("=" * 60)
    log.info(f"  Total      : {stats['total']}")
    log.info(f"  Procesados : {stats['ok']}")
    log.info(f"  Sin match  : {stats['sin_match']}")
    log.info(f"  Sin imagen : {stats['sin_imagen']}")
    log.info(f"  Errores    : {stats['errores']}")


if __name__ == "__main__":
    main()
