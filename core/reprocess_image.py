#!/usr/bin/env python3
"""
Reprocesa una imagen concreta de un producto Shopify.

Fuente (por orden de preferencia):
  1. backups/{slug}/{product_id}/img_{pos:02d}.*  (si existe backup)
  2. Descarga directa desde la URL actual en Shopify

Permite forzar el padding independientemente de la detección automática.
"""

import argparse
import logging
import sys
import time
from io import BytesIO
from pathlib import Path

import requests
from dotenv import load_dotenv
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.image_utils import process_image, process_image_webp_only, to_webp_b64
from core.shopify_api import ShopifyAPI, get_token
from core.process_brand import vendor_slug, title_slug

load_dotenv()

BACKUPS_DIR = Path("backups")
HEADERS = {"User-Agent": "Mozilla/5.0"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def load_image_from_backup(slug: str, product_id: int, position: int) -> Image.Image | None:
    folder = BACKUPS_DIR / slug / str(product_id)
    if not folder.exists():
        return None
    pattern = f"img_{position:02d}"
    for ext in ("jpg", "jpeg", "png", "webp"):
        path = folder / f"{pattern}.{ext}"
        if path.exists():
            log.info(f"  Fuente: backup → {path}")
            return Image.open(path)
    return None


def load_image_from_shopify(api: ShopifyAPI, product_id: int, position: int) -> Image.Image | None:
    images = api.get_images(product_id)
    target = next((img for img in images if img.get("position") == position), None)
    if not target:
        # fallback: ordenar por posición e indexar
        images_sorted = sorted(images, key=lambda x: x.get("position", 999))
        if position <= len(images_sorted):
            target = images_sorted[position - 1]
    if not target:
        log.error(f"  No existe imagen en posición {position}")
        return None
    url = target["src"].split("?")[0]
    log.warning(f"  Sin backup — descargando desde Shopify: {url.split('/')[-1]}")
    log.warning("  AVISO: esta es la imagen YA PROCESADA, no el original")
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return Image.open(BytesIO(r.content))


def replace_single_image(api: ShopifyAPI, product_id: int, position: int,
                          new_img: Image.Image, title: str, slug: str):
    images = api.get_images(product_id)
    images_sorted = sorted(images, key=lambda x: x.get("position", 999))

    target = next((img for img in images if img.get("position") == position), None)
    if not target and position <= len(images_sorted):
        target = images_sorted[position - 1]

    if target:
        alt = target.get("alt") or title
        api.delete_image(product_id, target["id"])
        log.info(f"  Imagen anterior en posición {position} eliminada")
        time.sleep(0.3)
    else:
        alt = title
        log.warning(f"  No se encontró imagen en posición {position} — se creará nueva")

    t_slug = title_slug(title)
    fname = f"{t_slug}_{position}.webp"
    api.upload_image(product_id, to_webp_b64(new_img), fname, alt=alt, position=position)
    log.info(f"  ✓ Subida: {fname} en posición {position}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor",      required=True)
    parser.add_argument("--product-id",  type=int, required=True)
    parser.add_argument("--position",    type=int, required=True,
                        help="Posición de la imagen (1-based)")
    parser.add_argument("--padding",     choices=["auto", "si", "no"], default="auto",
                        help="auto=detectar, si=forzar padding, no=forzar sin padding")
    parser.add_argument("--pipeline",    choices=["standard", "webp_only"], default="standard")
    args = parser.parse_args()

    force_padding = None if args.padding == "auto" else (args.padding == "si")

    log.info("=" * 60)
    log.info(f"  Vendor   : {args.vendor}")
    log.info(f"  Producto : {args.product_id}")
    log.info(f"  Posición : {args.position}")
    log.info(f"  Padding  : {args.padding}")
    log.info(f"  Pipeline : {args.pipeline}")
    log.info("=" * 60)

    token = get_token()
    api   = ShopifyAPI(token)
    slug  = vendor_slug(args.vendor)

    product = api.get_product(args.product_id)
    title   = product["title"]
    log.info(f"\n[{args.product_id}] {title}")

    img = load_image_from_backup(slug, args.product_id, args.position)
    if img is None:
        img = load_image_from_shopify(api, args.product_id, args.position)
    if img is None:
        log.error("No se pudo obtener la imagen")
        sys.exit(1)

    log.info(f"  Imagen cargada: {img.mode} {img.size}")

    if args.pipeline == "webp_only":
        processed = process_image_webp_only(img)
    else:
        processed = process_image(img, force_padding=force_padding)

    replace_single_image(api, args.product_id, args.position, processed, title, slug)
    log.info("\n✓ Completado")


if __name__ == "__main__":
    main()
