#!/usr/bin/env python3
"""
Procesador genérico de imágenes para Shopify
=============================================
Soporta tres fuentes de imágenes:

  shopify       Las imágenes ya están en Shopify. Se descargan, convierten
                a WebP 2000x2000 con fondo blanco y se re-suben.

  web_oficial   Las imágenes NO están en Shopify. Se scrapean del sitio
                oficial del fabricante (--web-url), se procesan y suben.

  web_y_amazon  Igual que web_oficial pero además se buscan imágenes
                adicionales en Amazon (listados del mismo producto) para
                disponer de más ángulos/presentaciones.

Uso:
  python3 scripts/process_generic.py \\
      --vendor "Royal Canin" \\
      --fuente shopify \\
      [--product-id 123456]

  python3 scripts/process_generic.py \\
      --vendor "Acana" \\
      --fuente web_oficial \\
      --web-url "https://www.acana.com/es/productos" \\
      [--product-id 123456] [--rebuild-catalog]

  python3 scripts/process_generic.py \\
      --vendor "Hills" \\
      --fuente web_y_amazon \\
      --web-url "https://www.hillspet.es/cat-food" \\
      [--product-id 123456] [--rebuild-catalog]
"""

import os
import sys
import time
import json
import base64
import logging
import argparse
import requests
from pathlib import Path
from io import BytesIO
from PIL import Image, ImageFilter
from dotenv import load_dotenv

load_dotenv()

# --- Configuración -----------------------------------------------------------

SHOP_DOMAIN    = os.getenv("SHOP_DOMAIN",   "7ev1zx-eg.myshopify.com")
CLIENT_ID      = os.getenv("CLIENT_ID",     "")
CLIENT_SECRET  = os.getenv("CLIENT_SECRET", "")
API_VERSION    = "2024-10"

TARGET_SIZE    = (2000, 2000)
WEBP_QUALITY   = 90
PADDING        = 0.05
WHITE_THRESH   = 245
WHITE_MIN_FRAC = 0.60

RESULTS_DIR    = Path("resultados")
ORIGINALS_DIR  = RESULTS_DIR / "originals_generic"
CATALOG_DIR    = RESULTS_DIR / "catalogs"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

# --- Logging -----------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# --- Autenticación Shopify ---------------------------------------------------

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
    return token

# --- Shopify API -------------------------------------------------------------

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

# --- Procesamiento de imágenes -----------------------------------------------

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
        (0, 0, patch, patch),
        (w - patch, 0, w, patch),
        (0, h - patch, patch, h),
        (w - patch, h - patch, w, h),
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
    composited = (_fill_transparent_with_blur(img.convert("RGBA"))
                  if has_alpha else _composite_on_white(img))

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


def download_raw(url: str) -> tuple[bytes, str]:
    clean = url.split("?")[0]
    r = requests.get(clean, timeout=60, headers=HEADERS)
    r.raise_for_status()
    ext = clean.rsplit(".", 1)[-1].lower()
    ext = "jpg" if ext not in ("jpg", "jpeg", "png", "webp", "gif") or ext == "jpeg" else ext
    return r.content, ext

# --- Fuente: SHOPIFY ---------------------------------------------------------

def get_images_from_shopify(api: ShopifyAPI, pid: int) -> list[tuple[bytes, str]]:
    """Descarga las imágenes actuales de Shopify para un producto."""
    imgs = api.get_images(pid)
    result = []
    for img_data in imgs:
        raw, ext = download_raw(img_data["src"])
        result.append((raw, ext))
    return result

# --- Fuente: WEB OFICIAL -----------------------------------------------------
# TODO: implementar scraping específico por marca.
# Devuelve lista de (bytes, ext) con las imágenes del producto.

def get_images_from_web(product: dict, web_url: str,
                        catalog: dict) -> list[tuple[bytes, str]]:
    """
    Busca y descarga imágenes del sitio oficial del fabricante.

    Pasos a implementar por marca:
      1. Buscar el producto en `catalog` (dict pre-scrapeado de web_url).
      2. Obtener las URLs de imagen del producto encontrado.
      3. Descargar y devolver los bytes.

    `catalog` tiene estructura: { handle: { "name": str, "images": [url, ...] } }
    """
    log.warning(f"  [web_oficial] Scraping no implementado para este vendor.")
    log.warning(f"  Adapta get_images_from_web() en scripts/process_generic.py")
    return []

# --- Fuente: WEB + AMAZON ----------------------------------------------------
# TODO: buscar en Amazon por título/EAN y extraer imágenes del carrusel.

def get_images_from_amazon(product: dict) -> list[tuple[bytes, str]]:
    """
    Busca imágenes adicionales en Amazon para el producto dado.
    Estrategia sugerida:
      1. Buscar EAN o título en amazon.es/s?k=<titulo>
      2. Acceder al listing, extraer JSON de imágenes del carrusel
      3. Descargar en máxima resolución
    """
    log.warning("  [web_y_amazon] Búsqueda en Amazon no implementada.")
    return []

# --- Catálogo web (caché) ----------------------------------------------------

def load_catalog(vendor_slug: str) -> dict:
    path = CATALOG_DIR / f"{vendor_slug}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_catalog(vendor_slug: str, catalog: dict):
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    path = CATALOG_DIR / f"{vendor_slug}.json"
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"  Catálogo guardado: {path}")


def build_catalog(web_url: str, vendor_slug: str) -> dict:
    """
    Scrapea el sitio oficial y construye el catálogo.
    TODO: implementar por marca según la estructura HTML de cada fabricante.
    Devuelve: { handle: { "name": str, "images": [url, ...] } }
    """
    log.warning(f"  build_catalog() no implementado para {vendor_slug}.")
    log.warning(f"  Implementa el scraping de {web_url} en scripts/process_generic.py")
    return {}

# --- Backup local ------------------------------------------------------------

def save_originals(vendor_slug: str, pid: int, images: list[tuple[bytes, str]]):
    folder = ORIGINALS_DIR / vendor_slug / str(pid)
    folder.mkdir(parents=True, exist_ok=True)
    for i, (raw, ext) in enumerate(images, 1):
        (folder / f"img_{i:02d}.{ext}").write_bytes(raw)

# --- Bucle principal ---------------------------------------------------------

def process_product(api: ShopifyAPI, product: dict, fuente: str,
                    web_url: str, catalog: dict, vendor_slug: str) -> bool:
    pid   = product["id"]
    title = product["title"]
    log.info(f"  Título : {title}")

    if fuente == "shopify":
        raw_images = get_images_from_shopify(api, pid)
        if not raw_images:
            log.warning("  Sin imágenes en Shopify — saltando")
            return False

    elif fuente == "web_oficial":
        raw_images = get_images_from_web(product, web_url, catalog)
        if not raw_images:
            log.warning("  Sin imágenes encontradas en web oficial — saltando")
            return False

    elif fuente == "web_y_amazon":
        raw_images = get_images_from_web(product, web_url, catalog) + \
                     get_images_from_amazon(product)
        if not raw_images:
            log.warning("  Sin imágenes encontradas (web + Amazon) — saltando")
            return False
    else:
        log.error(f"  Fuente desconocida: {fuente}")
        return False

    log.info(f"  {len(raw_images)} imagen(es) obtenida(s)")
    save_originals(vendor_slug, pid, raw_images)

    processed = []
    for i, (raw, _) in enumerate(raw_images, 1):
        try:
            img = Image.open(BytesIO(raw))
            log.info(f"  Imagen {i}: {img.mode} {img.size}")
            processed.append(process_image(img))
        except Exception as e:
            log.warning(f"  Error procesando imagen {i}: {e}")

    if not processed:
        log.warning("  Ninguna imagen procesada — saltando")
        return False

    existing = api.get_images(pid)
    for img_data in existing:
        api.delete_image(pid, img_data["id"])
        time.sleep(0.2)
    log.info(f"  {len(existing)} imagen(es) antigua(s) eliminada(s)")

    for pos, img in enumerate(processed, 1):
        fname = f"{vendor_slug}_{pid}_{pos}.webp"
        api.upload_image(pid, to_webp_b64(img), fname, alt=title, position=pos)
        log.info(f"  Imagen {pos}/{len(processed)} subida")
        time.sleep(0.5)

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Procesador genérico de imágenes para Shopify")
    parser.add_argument("--vendor",          required=True)
    parser.add_argument("--fuente",          required=True,
                        choices=["shopify", "web_oficial", "web_y_amazon"])
    parser.add_argument("--web-url",         default="")
    parser.add_argument("--product-id",      type=int, default=None)
    parser.add_argument("--rebuild-catalog", action="store_true")
    args = parser.parse_args()

    if not CLIENT_ID or not CLIENT_SECRET:
        log.error("Faltan CLIENT_ID / CLIENT_SECRET")
        sys.exit(1)
    if args.fuente != "shopify" and not args.web_url:
        log.error("--web-url es obligatorio cuando fuente != shopify")
        sys.exit(1)

    RESULTS_DIR.mkdir(exist_ok=True)
    vendor_slug = args.vendor.lower().replace(" ", "_")

    log.info("=" * 60)
    log.info("PROCESADOR GENÉRICO — SHOPIFY")
    log.info("=" * 60)
    log.info(f"Vendor  : {args.vendor}")
    log.info(f"Fuente  : {args.fuente}")
    if args.web_url:
        log.info(f"Web URL : {args.web_url}")

    token = get_token()
    api   = ShopifyAPI(token)
    log.info("Token obtenido")

    catalog = {}
    if args.fuente in ("web_oficial", "web_y_amazon"):
        cache_path = CATALOG_DIR / f"{vendor_slug}.json"
        if args.rebuild_catalog or not cache_path.exists():
            log.info("Construyendo catálogo web...")
            catalog = build_catalog(args.web_url, vendor_slug)
            if catalog:
                save_catalog(vendor_slug, catalog)
        else:
            catalog = load_catalog(vendor_slug)
            log.info(f"Catálogo cargado desde caché: {len(catalog)} entradas")

    if args.product_id:
        log.info(f"Modo prueba — producto ID: {args.product_id}")
        products = [api.get_product(args.product_id)]
    else:
        log.info(f"Cargando todos los productos del vendor '{args.vendor}'...")
        products = api.get_products(args.vendor)
        log.info(f"Total: {len(products)} productos")

    if not products:
        log.warning("Sin productos encontrados.")
        return

    stats = dict(total=len(products), ok=0, sin_imagen=0, errores=0)

    for i, product in enumerate(products, 1):
        log.info(f"\n[{i}/{len(products)}] ID {product['id']}")
        try:
            ok = process_product(api, product, args.fuente,
                                 args.web_url, catalog, vendor_slug)
            stats["ok" if ok else "sin_imagen"] += 1
        except Exception as e:
            log.error(f"  ERROR: {e}")
            stats["errores"] += 1
        time.sleep(1)

    log.info("\n" + "=" * 60)
    log.info("RESUMEN")
    log.info("=" * 60)
    log.info(f"  Total     : {stats['total']}")
    log.info(f"  Procesados: {stats['ok']}")
    log.info(f"  Sin imagen: {stats['sin_imagen']}")
    log.info(f"  Errores   : {stats['errores']}")


if __name__ == "__main__":
    main()
