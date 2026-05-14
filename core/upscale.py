"""
Super-resolución con OpenCV DNN + modelo EDSR.

Funciona en CPU puro, sin GPU ni Vulkan. EDSR (Enhanced Deep Residual
Networks) es un modelo de super-resolución de referencia en la industria:
mejora nitidez, elimina artefactos JPG y recupera detalles finos.
"""

import argparse
import logging
import os
import sys
import time
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
import requests
from dotenv import load_dotenv
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.image_utils import process_image, to_webp_b64
from core.shopify_api import ShopifyAPI, get_token
from core.process_brand import vendor_slug, _replace_images

load_dotenv()

BACKUPS_DIR = Path("backups")
MODELS_DIR  = Path("models")

EDSR_MODELS = {
    2: "https://github.com/Saafke/EDSR_Tensorflow/raw/master/models/EDSR_x2.pb",
    4: "https://github.com/Saafke/EDSR_Tensorflow/raw/master/models/EDSR_x4.pb",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def load_sr_model(scale: int = 2):
    """Descarga si hace falta y carga el modelo EDSR."""
    MODELS_DIR.mkdir(exist_ok=True)
    model_path = MODELS_DIR / f"EDSR_x{scale}.pb"

    if not model_path.exists():
        url = EDSR_MODELS[scale]
        log.info(f"Descargando modelo EDSR x{scale} (~150 MB)...")
        r = requests.get(url, stream=True, timeout=300)
        r.raise_for_status()
        model_path.write_bytes(r.content)
        log.info("Modelo descargado.")

    sr = cv2.dnn_superres.DnnSuperResImpl_create()
    sr.readModel(str(model_path))
    sr.setModel("edsr", scale)
    log.info(f"Modelo EDSR x{scale} cargado (CPU).")
    return sr


def upscale_pil(img: Image.Image, sr, scale: int) -> Image.Image:
    """Aplica EDSR super-resolución a una imagen PIL."""
    img_np  = np.array(img.convert("RGB"))
    img_bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)

    log.info(f"    [EDSR x{scale}] {img.size} → procesando en CPU...")
    upscaled_bgr = sr.upsample(img_bgr)

    upscaled_rgb = cv2.cvtColor(upscaled_bgr, cv2.COLOR_BGR2RGB)
    result = Image.fromarray(upscaled_rgb)
    log.info(f"    [EDSR] {img.size} → {result.size}")
    return result


def run_upscale(api: ShopifyAPI, vendor: str,
                product_ids: list, scale: int = 2):
    slug        = vendor_slug(vendor)
    backup_root = BACKUPS_DIR / slug

    if not backup_root.exists():
        log.error(f"No existe backup para '{vendor}'.")
        sys.exit(1)

    sr = load_sr_model(scale)

    for pid in product_ids:
        product = api.get_product(pid)
        title   = product["title"]
        log.info(f"\n[{pid}] {title}")

        folder = backup_root / str(pid)
        if not folder.exists() or not any(folder.iterdir()):
            log.warning("  Sin backup — saltando")
            continue

        processed = []
        for img_path in sorted(folder.iterdir()):
            if img_path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                continue
            try:
                raw = img_path.read_bytes()
                img = Image.open(BytesIO(raw)).convert("RGB")
                log.info(f"  {img_path.name}: {img.size}")

                upscaled = upscale_pil(img, sr, scale)
                final    = process_image(upscaled)
                processed.append(final)
            except Exception as e:
                log.warning(f"  Error en {img_path.name}: {e}")

        if not processed:
            log.warning("  Ninguna imagen procesada — saltando")
            continue

        _replace_images(api, pid, title, slug, processed)
        log.info(f"  ✓ {len(processed)} imagen(es) mejoradas y subidas")
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor",      required=True)
    parser.add_argument("--product-ids", required=True)
    parser.add_argument("--scale",       type=int, default=2, choices=[2, 4])
    args = parser.parse_args()

    product_ids = [int(x.strip()) for x in args.product_ids.split(",") if x.strip()]

    log.info("=" * 60)
    log.info(f"  Vendor : {args.vendor}")
    log.info(f"  IDs    : {product_ids}")
    log.info(f"  Scale  : x{args.scale}")
    log.info("=" * 60)

    token = get_token()
    api   = ShopifyAPI(token)
    run_upscale(api, args.vendor, product_ids, scale=args.scale)


if __name__ == "__main__":
    main()
