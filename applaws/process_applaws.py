#!/usr/bin/env python3
"""
Procesador de imágenes Applaws para Shopify
============================================
Las imágenes ya están en Shopify con alta calidad.
El script descarga cada imagen, la procesa (2000x2000 WebP,
fondo blanco) y la re-sube reemplazando la original.

Regla de padding:
  - Fondo blanco (>=60% de esquinas blancas) -> 5% padding
  - Ilustraciones / fondo de color -> sin padding, rellena 2000x2000
  La detección usa parches del 5% en las 4 esquinas. Las fotos de
  producto tienen esquinas blancas (70-100%); las ilustraciones, 0%.

Backup automático:
  Antes de borrar las imágenes de Shopify, el script guarda los
  originales en resultados/originals_applaws/PRODUCT_ID/.
  Esto permite re-procesar desde los originales con --from-originals.

Uso:
  python3 applaws/process_applaws.py                          # todos los productos
  python3 applaws/process_applaws.py --product-id 123456      # modo prueba
  python3 applaws/process_applaws.py --only-ids 123,456       # lista especifica
  python3 applaws/process_applaws.py --product-id 123456 --from-originals  # re-test desde backup
  PRODUCT_IDS=123,456 python3 applaws/process_applaws.py      # via env var
"""

import os
import sys
import time
import base64
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
VENDOR         = "Applaws"
API_VERSION    = "2024-10"
TARGET_SIZE    = (2000, 2000)
WEBP_QUALITY   = 90
OUTPUT_DIR     = Path("imagenes_applaws")
ORIGINALS_DIR  = Path("resultados/originals_applaws")
PADDING        = 0.05
WHITE_THRESH   = 245   # un píxel es "blanco" si todos sus canales >= este valor
WHITE_MIN_FRAC = 0.60  # fotos producto: 70-100%; ilustraciones: 0% -> umbral 60%

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
}

# --- Logging -----------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("applaws_images.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# --- Autenticacion Shopify ---------------------------------------------------

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
        raise ValueError(f"Error al obtener token: {resp.text}")
    log.info("Access token obtenido")
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
            resp = requests.get(url, headers=self.h, params=params, timeout=30)
            resp.raise_for_status()
            products.extend(resp.json().get("products", []))
            log.info(f"  Productos cargados: {len(products)}")
            params, url = {}, None
            link = resp.headers.get("Link", "")
            if 'rel="next"' in link:
                for part in link.split(","):
                    if 'rel="next"' in part:
                        url = part.strip().split(";")[0].strip("<>")
        return products

    def get_product(self, product_id: int) -> dict:
        resp = requests.get(f"{self.base}/products/{product_id}.json",
                            headers=self.h, timeout=30)
        resp.raise_for_status()
        return resp.json()["product"]

    def get_images(self, product_id: int) -> list:
        resp = requests.get(f"{self.base}/products/{product_id}/images.json",
                            headers=self.h, timeout=30)
        resp.raise_for_status()
        return resp.json().get("images", [])

    def delete_image(self, product_id: int, image_id: int):
        requests.delete(
            f"{self.base}/products/{product_id}/images/{image_id}.json",
            headers=self.h, timeout=30,
        ).raise_for_status()

    def create_image(self, product_id: int, b64: str, filename: str,
                     alt: str = "", position: int = None) -> dict:
        payload = {"attachment": b64, "filename": filename, "alt": alt}
        if position is not None:
            payload["position"] = position
        resp = requests.post(
            f"{self.base}/products/{product_id}/images.json",
            headers=self.h, json={"image": payload}, timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

# --- Backup de originales ----------------------------------------------------

def save_originals(pid: int, images_bytes: list[tuple[bytes, str]]) -> Path:
    folder = ORIGINALS_DIR / str(pid)
    folder.mkdir(parents=True, exist_ok=True)
    for i, (raw, ext) in enumerate(images_bytes, 1):
        dest = folder / f"img_{i:02d}.{ext}"
        dest.write_bytes(raw)
        log.info(f"  Backup original {i}: {dest.name}  ({len(raw)//1024} KB)")
    return folder


def load_originals(pid: int) -> list[tuple[bytes, str]] | None:
    folder = ORIGINALS_DIR / str(pid)
    if not folder.exists():
        return None
    files = sorted(f for f in folder.iterdir() if f.name.startswith("img_"))
    if not files:
        return None
    return [(f.read_bytes(), f.suffix.lstrip(".")) for f in files]

# --- Procesamiento de imagenes -----------------------------------------------

def _composite_on_white(img: Image.Image) -> Image.Image:
    """Devuelve RGB compuesto sobre blanco (para imágenes sin alpha)."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba.convert("RGB"), mask=rgba.split()[3])
        return bg
    return img.convert("RGB")


def _fill_transparent_with_blur(img_rgba: Image.Image) -> Image.Image:
    """
    Rellena las áreas transparentes (ej.: esquinas redondeadas) con el color
    de los bordes adyacentes, usando desenfoque gaussiano. Evita esquinas
    blancas visibles en banners/ilustraciones con bordes redondeados.
    """
    alpha = img_rgba.split()[3]
    rgb = img_rgba.convert("RGB")
    blurred = rgb.filter(ImageFilter.GaussianBlur(radius=40))
    result = blurred.copy()
    result.paste(rgb, (0, 0), mask=alpha)
    return result


def _is_white_background(img_rgb: Image.Image) -> bool:
    """
    Comprueba las 4 esquinas de la imagen (parche del 5% del lado menor).
    Las fotos de producto en fondo blanco tienen esquinas blancas (70-100%)
    aunque el producto llegue a los bordes. Las ilustraciones tienen 0%.
    Umbral: 60%, que crea separación clara entre ambos tipos.
    """
    w, h = img_rgb.size
    patch = max(20, int(min(w, h) * 0.05))
    step = 2
    total = 0
    white = 0

    corners = [
        (0,         0,         patch,     patch),
        (w - patch, 0,         w,         patch),
        (0,         h - patch, patch,     h),
        (w - patch, h - patch, w,         h),
    ]
    for x1, y1, x2, y2 in corners:
        for x in range(x1, x2, step):
            for y in range(y1, y2, step):
                r, g, b = img_rgb.getpixel((x, y))
                white += int(r >= WHITE_THRESH and g >= WHITE_THRESH and b >= WHITE_THRESH)
                total += 1

    ratio = white / total if total > 0 else 0.0
    log.info(f"    [bg check esquinas] {white}/{total} px blancos ({ratio:.0%})")
    return ratio >= WHITE_MIN_FRAC


def process_image(img: Image.Image) -> Image.Image:
    """
    Redimensiona a 2000x2000 con fondo blanco.
    - Fondo blanco (>=60% esquinas blancas): aplica 5% de padding en cada lado.
    - Ilustraciones / fondo de color: rellena los 2000x2000 sin margen.
    """
    has_alpha = (img.mode in ("RGBA", "LA") or
                 (img.mode == "P" and "transparency" in img.info))
    if has_alpha:
        composited = _fill_transparent_with_blur(img.convert("RGBA"))
    else:
        composited = _composite_on_white(img)

    use_padding = _is_white_background(composited)
    label = "fondo blanco -> padding 5%" if use_padding else "ilustracion -> sin padding"
    log.info(f"    [{label}]")

    if use_padding:
        max_w = int(TARGET_SIZE[0] * (1 - 2 * PADDING))  # 1900 px
        max_h = int(TARGET_SIZE[1] * (1 - 2 * PADDING))  # 1900 px
    else:
        max_w, max_h = TARGET_SIZE  # 2000 px

    ratio = composited.width / composited.height
    if ratio > 1:
        new_w, new_h = max_w, int(max_w / ratio)
    else:
        new_w, new_h = int(max_h * ratio), max_h

    resized    = composited.resize((new_w, new_h), Image.LANCZOS)
    background = Image.new("RGB", TARGET_SIZE, (255, 255, 255))
    ox = (TARGET_SIZE[0] - new_w) // 2
    oy = (TARGET_SIZE[1] - new_h) // 2
    background.paste(resized, (ox, oy))
    return background


def to_b64_webp(img: Image.Image) -> str:
    buf = BytesIO()
    img.save(buf, format="WEBP", quality=WEBP_QUALITY, method=6)
    return base64.b64encode(buf.getvalue()).decode()


def fetch_image_raw(url: str) -> tuple[bytes, str]:
    """Descarga la imagen original y retorna (bytes_raw, extension)."""
    clean_url = url.split("?")[0]
    resp = requests.get(clean_url, timeout=60, headers=HEADERS)
    resp.raise_for_status()
    ext = clean_url.split(".")[-1].lower()
    if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
        ext = "jpg"
    ext = "jpg" if ext == "jpeg" else ext
    return resp.content, ext


def image_from_bytes(raw: bytes) -> Image.Image:
    return Image.open(BytesIO(raw))

# --- Flujo principal ---------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Procesa imagenes Applaws en Shopify: 2000x2000 WebP, fondo blanco")
    parser.add_argument("--product-id", type=int, default=None,
                        help="Procesar solo este product ID (modo prueba)")
    parser.add_argument("--only-ids", type=str, default=None,
                        help="Lista de IDs separados por coma (ej: 123,456)")
    parser.add_argument("--from-originals", action="store_true",
                        help="Re-procesar desde el backup guardado en resultados/originals_applaws/")
    args = parser.parse_args()

    if not CLIENT_ID or not CLIENT_SECRET:
        log.error("Faltan CLIENT_ID o CLIENT_SECRET. Revisa el archivo .env")
        sys.exit(1)

    OUTPUT_DIR.mkdir(exist_ok=True)
    ORIGINALS_DIR.mkdir(parents=True, exist_ok=True)
    Path("resultados").mkdir(exist_ok=True)

    log.info("=" * 60)
    log.info("PROCESADOR APPLAWS - SHOPIFY")
    log.info("=" * 60)
    log.info(f"Tienda  : {SHOP_DOMAIN}")
    log.info(f"Vendor  : {VENDOR}")
    log.info(f"Formato : WebP calidad {WEBP_QUALITY}, 2000x2000")
    log.info(f"Padding : {int(PADDING*100)}% solo en imagenes con fondo blanco (umbral {int(WHITE_MIN_FRAC*100)}%)")
    if args.from_originals:
        log.info("Modo    : --from-originals (re-procesando desde backup local)")

    token = get_token()
    api   = ShopifyAPI(token)

    only_ids_raw = args.only_ids or os.getenv("PRODUCT_IDS", "")
    only_ids: set[int] = set()
    if only_ids_raw:
        only_ids = {int(x.strip()) for x in only_ids_raw.split(",") if x.strip()}
        log.info(f"Modo re-proceso - {len(only_ids)} IDs especificos")

    if args.product_id:
        log.info(f"Modo prueba - producto ID: {args.product_id}")
        products = [api.get_product(args.product_id)]
    elif only_ids:
        log.info(f"Obteniendo {len(only_ids)} productos especificos...")
        products = [api.get_product(pid) for pid in only_ids]
        log.info(f"Total a procesar: {len(products)} productos\n")
    else:
        log.info("Obteniendo todos los productos Applaws de Shopify...")
        products = api.get_products(VENDOR)
        log.info(f"Total: {len(products)} productos\n")

    if not products:
        log.warning("Sin productos encontrados.")
        return

    stats = dict(total=len(products), actualizadas=0, sin_imagen=0, errores=0)

    for i, product in enumerate(products, 1):
        pid   = product["id"]
        title = product["title"]
        log.info(f"\n[{i}/{len(products)}] {title}  (ID: {pid})")

        try:
            if args.from_originals:
                originals = load_originals(pid)
                if not originals:
                    log.error(f"  No hay backup en {ORIGINALS_DIR / str(pid)} - saltando")
                    stats["errores"] += 1
                    continue
                log.info(f"  Cargando {len(originals)} original(es) desde backup local")
                images_raw     = originals
                shopify_images = api.get_images(pid)
            else:
                shopify_images = api.get_images(pid)
                if not shopify_images:
                    log.warning("  Sin imagenes en Shopify - saltando")
                    stats["sin_imagen"] += 1
                    continue

                log.info(f"  {len(shopify_images)} imagen(es) - descargando originales...")
                images_raw = []
                for img_data in shopify_images:
                    raw, ext = fetch_image_raw(img_data["src"])
                    images_raw.append((raw, ext))
                    log.info(f"  Descargada: {img_data['src'].split('/')[-1].split('?')[0]}")

                save_originals(pid, images_raw)

            processed_images = []
            for j, (raw, _) in enumerate(images_raw):
                try:
                    img       = image_from_bytes(raw)
                    processed = process_image(img)
                    alt = shopify_images[j].get("alt") or title if j < len(shopify_images) else title
                    processed_images.append({"processed": processed, "alt": alt})
                except Exception as exc:
                    log.warning(f"  No se pudo procesar imagen {j+1}: {exc}")

            if not processed_images:
                log.warning("  Ninguna imagen procesada - saltando")
                stats["errores"] += 1
                continue

            for img_data in shopify_images:
                api.delete_image(pid, img_data["id"])
                time.sleep(0.2)
            log.info(f"  {len(shopify_images)} imagen(es) antigua(s) eliminada(s)")

            for pos, item in enumerate(processed_images, 1):
                b64   = to_b64_webp(item["processed"])
                fname = f"applaws_{pid}_{pos}.webp"
                item["processed"].save(
                    OUTPUT_DIR / fname, format="WEBP",
                    quality=WEBP_QUALITY, method=6,
                )
                api.create_image(pid, b64, fname, alt=item["alt"], position=pos)
                log.info(f"  Imagen {pos}/{len(processed_images)} subida -> {fname}")
                time.sleep(0.5)

            stats["actualizadas"] += 1
            log.info("  Producto completado")

        except Exception as exc:
            log.error(f"  ERROR: {exc}")
            stats["errores"] += 1

        time.sleep(1)

    log.info("\n" + "=" * 60)
    log.info("RESUMEN FINAL")
    log.info("=" * 60)
    log.info(f"  Productos procesados  : {stats['total']}")
    log.info(f"  Imagenes actualizadas : {stats['actualizadas']}")
    log.info(f"  Sin imagen en Shopify : {stats['sin_imagen']}")
    log.info(f"  Errores               : {stats['errores']}")
    log.info(f"  Copias locales        -> {OUTPUT_DIR}/")
    log.info(f"  Originales backup     -> {ORIGINALS_DIR}/")
    log.info(f"  Log completo          -> applaws_images.log")


if __name__ == "__main__":
    main()
