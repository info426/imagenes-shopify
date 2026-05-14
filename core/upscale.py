"""
Super-resolución con Real-ESRGAN.

Mejora la calidad de píxeles de imágenes de producto:
- Elimina artefactos de compresión JPG
- Afila bordes y recupera detalles finos
- El resultado exportado a WebP 80 ocupa peso similar o menor al original

Uso:
    python3 core/upscale.py --vendor "Alpha Spirit" --product-ids 15509626356099
"""

import argparse
import logging
import os
import sys
import time
from io import BytesIO
from pathlib import Path

import requests
from dotenv import load_dotenv
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.image_utils import process_image, to_webp_b64
from core.shopify_api import ShopifyAPI, get_token
from core.process_brand import vendor_slug, title_slug, _replace_images

load_dotenv()

BACKUPS_DIR = Path("backups")
MODELS_DIR  = Path("models")

# Modelo más ligero de Real-ESRGAN, equilibrio calidad/velocidad en CPU
MODEL_URL  = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth"
MODEL_NAME = "realesr-general-x4v3.pth"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def download_model() -> Path:
    """Descarga el modelo Real-ESRGAN si no existe."""
    MODELS_DIR.mkdir(exist_ok=True)
    model_path = MODELS_DIR / MODEL_NAME
    if not model_path.exists():
        log.info(f"Descargando modelo {MODEL_NAME} (~60 MB)...")
        r = requests.get(MODEL_URL, stream=True, timeout=120)
        r.raise_for_status()
        model_path.write_bytes(r.content)
        log.info("Modelo descargado.")
    return model_path


def load_upscaler(model_path: Path):
    """Inicializa Real-ESRGAN para CPU."""
    import torch
    from basicsr.archs.srvgg_arch import SRVGGNetCompact
    from realesrgan import RealESRGANer

    model = SRVGGNetCompact(
        num_in_ch=3, num_out_ch=3,
        num_feat=64, num_conv=32,
        upscale=4, act_type="prelu",
    )
    return RealESRGANer(
        scale=4,
        model_path=str(model_path),
        model=model,
        tile=256,       # procesa en tiles para no agotar RAM en CPU
        tile_pad=10,
        pre_pad=0,
        half=False,     # CPU no soporta FP16
        device=torch.device("cpu"),
    )


def upscale_pil(img: Image.Image, upsampler, outscale: float = 2.0) -> Image.Image:
    """Aplica super-resolución Real-ESRGAN a una imagen PIL."""
    import cv2
    import numpy as np

    img_np  = np.array(img.convert("RGB"))
    img_bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)

    log.info(f"    [ESRGAN] procesando {img.size} → outscale x{outscale}...")
    output_bgr, _ = upsampler.enhance(img_bgr, outscale=outscale)

    output_rgb = cv2.cvtColor(output_bgr, cv2.COLOR_BGR2RGB)
    result = Image.fromarray(output_rgb)
    log.info(f"    [ESRGAN] {img.size} → {result.size}")
    return result


def run_upscale(api: ShopifyAPI, vendor: str,
                product_ids: list, outscale: float = 2.0):
    """Descarga originales del backup, aplica ESRGAN y sube a Shopify."""
    slug     = vendor_slug(vendor)
    backup_root = BACKUPS_DIR / slug

    if not backup_root.exists():
        log.error(f"No existe backup para '{vendor}'.")
        sys.exit(1)

    model_path = download_model()
    upsampler  = load_upscaler(model_path)
    log.info("Modelo Real-ESRGAN cargado.")

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

                # 1. Super-resolución
                upscaled = upscale_pil(img, upsampler, outscale=outscale)

                # 2. Pipeline estándar (autocrop + padding + 2000×2000)
                final = process_image(upscaled)
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
    parser.add_argument("--product-ids", required=True,
                        help="IDs separados por coma")
    parser.add_argument("--outscale",    type=float, default=2.0,
                        help="Factor de escala ESRGAN (default: 2)")
    args = parser.parse_args()

    product_ids = [int(x.strip()) for x in args.product_ids.split(",") if x.strip()]

    log.info("=" * 60)
    log.info(f"  Vendor   : {args.vendor}")
    log.info(f"  IDs      : {product_ids}")
    log.info(f"  Outscale : x{args.outscale}")
    log.info("=" * 60)

    token = get_token()
    api   = ShopifyAPI(token)
    run_upscale(api, args.vendor, product_ids, outscale=args.outscale)


if __name__ == "__main__":
    main()
