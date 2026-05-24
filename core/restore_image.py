#!/usr/bin/env python3
"""
Restaura la calidad de imágenes degradadas de productos Shopify.

Para imágenes que perdieron nitidez por compresión, reescalado o ediciones
repetidas. Pipeline de restauración (en este orden):

  1. Denoise (cv2.fastNlMeansDenoisingColored) — elimina ruido y bloques JPG
     ANTES de ampliar, para no amplificar el ruido.
  2. Super-resolución EDSR x2/x4 — elimina pixelado y recupera detalle fino.
  3. Unsharp mask — aumenta la nitidez percibida sobre la imagen ya ampliada.
  4. process_image — estándar de tienda (2000×2000 WebP).

Origen: imagen viva del producto en Shopify (la que perdió calidad).
Destino: reemplaza esa misma imagen en su posición, conservando alt.
"""

import argparse
import logging
import sys
import time
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
import requests
from dotenv import load_dotenv
from PIL import Image, ImageFilter

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.image_utils import process_image, to_webp_b64, to_webp_srgb_b64
from core.shopify_api import ShopifyAPI, get_token
from core.process_brand import title_slug
from core.upscale import load_sr_model, upscale_pil

load_dotenv()

HEADERS = {"User-Agent": "Mozilla/5.0"}

# Nivel → (h luminancia, hColor) para fastNlMeansDenoisingColored
DENOISE_LEVELS = {"low": (3, 3), "medium": (7, 7), "high": (12, 10)}
# Nivel → percent del UnsharpMask (radius/threshold fijos para no realzar ruido)
SHARPEN_LEVELS = {"low": 80, "medium": 150, "high": 250}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def denoise_pil(img: Image.Image, level: str) -> Image.Image:
    """Reduce ruido y artefactos de compresión preservando bordes."""
    if level == "off":
        return img.convert("RGB")
    h, hcolor = DENOISE_LEVELS[level]
    bgr = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    out = cv2.fastNlMeansDenoisingColored(bgr, None, h, hcolor, 7, 21)
    log.info(f"    [denoise {level}] h={h} hColor={hcolor}")
    return Image.fromarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB))


def sharpen_pil(img: Image.Image, level: str) -> Image.Image:
    """Aumenta nitidez con unsharp mask. threshold=3 evita realzar ruido plano."""
    if level == "off":
        return img
    percent = SHARPEN_LEVELS[level]
    log.info(f"    [sharpen {level}] percent={percent}")
    return img.filter(ImageFilter.UnsharpMask(radius=2, percent=percent, threshold=3))


def restore_image(img: Image.Image, sr, scale: int,
                  denoise_level: str, sharpen_level: str) -> Image.Image:
    """Denoise → super-resolución EDSR → unsharp mask."""
    rgb = denoise_pil(img, denoise_level)
    rgb = upscale_pil(rgb, sr, scale)
    rgb = sharpen_pil(rgb, sharpen_level)
    return rgb


def run_restore(api: ShopifyAPI, vendor: str, product_ids: list, scale: int,
                denoise_level: str, sharpen_level: str,
                force_padding: bool | None, srgb: bool):
    sr      = load_sr_model(scale)
    encoder = to_webp_srgb_b64 if srgb else to_webp_b64

    for pid in product_ids:
        product = api.get_product(pid)
        title   = product["title"]
        images  = sorted(api.get_images(pid), key=lambda x: x.get("position", 999))
        log.info(f"\n[{pid}] {title} — {len(images)} imágenes")

        if not images:
            log.warning("  Sin imágenes — saltando")
            continue

        for meta in images:
            pos = meta.get("position")
            alt = meta.get("alt") or title
            url = meta["src"].split("?")[0]
            try:
                r = requests.get(url, headers=HEADERS, timeout=60)
                r.raise_for_status()
                img = Image.open(BytesIO(r.content))
                log.info(f"  pos {pos}: {img.mode} {img.size} ← {url.split('/')[-1]}")

                restored = restore_image(img, sr, scale, denoise_level, sharpen_level)
                final    = process_image(restored, force_padding=force_padding)

                api.delete_image(pid, meta["id"])
                time.sleep(0.3)
                fname = f"{title_slug(title)}_{pos}.webp"
                api.upload_image(pid, encoder(final), fname, alt=alt, position=pos)
                log.info(f"    ✓ restaurada y subida [{fname}] en posición {pos}")
                time.sleep(0.5)
            except Exception as e:
                log.warning(f"  pos {pos}: error — {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor",        required=True)
    parser.add_argument("--product-ids",   required=True,
                        help="IDs de productos separados por coma")
    parser.add_argument("--scale",         type=int, default=2, choices=[2, 4])
    parser.add_argument("--denoise",       choices=["off", "low", "medium", "high"],
                        default="medium")
    parser.add_argument("--sharpen",       choices=["off", "low", "medium", "high"],
                        default="medium")
    parser.add_argument("--force-padding", choices=["auto", "true", "false"], default="auto")
    parser.add_argument("--srgb",          action="store_true")
    args = parser.parse_args()

    product_ids = [int(x.strip()) for x in args.product_ids.split(",") if x.strip()]
    force_padding: bool | None = {"auto": None, "true": True, "false": False}[args.force_padding]

    log.info("=" * 60)
    log.info(f"  Vendor   : {args.vendor}")
    log.info(f"  IDs      : {product_ids}")
    log.info(f"  Scale    : x{args.scale}")
    log.info(f"  Denoise  : {args.denoise}")
    log.info(f"  Sharpen  : {args.sharpen}")
    log.info(f"  Padding  : {args.force_padding}")
    log.info(f"  sRGB     : {'sí' if args.srgb else 'no'}")
    log.info("=" * 60)

    token = get_token()
    api   = ShopifyAPI(token)
    run_restore(api, args.vendor, product_ids, args.scale,
                args.denoise, args.sharpen, force_padding, args.srgb)
    log.info("\n✓ Completado")


if __name__ == "__main__":
    main()
