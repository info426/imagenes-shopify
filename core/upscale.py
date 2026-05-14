"""
Super-resolución con Real-ESRGAN ncnn-vulkan (binario precompilado).

Sin dependencias PyTorch/basicsr — usa el binario oficial de xinntao.
Mismo modelo y calidad que la versión Python, sin conflictos de librerías.
"""

import argparse
import logging
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from io import BytesIO
from pathlib import Path

import requests
from dotenv import load_dotenv
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.image_utils import process_image, to_webp_b64
from core.shopify_api import ShopifyAPI, get_token
from core.process_brand import vendor_slug, _replace_images

load_dotenv()

BACKUPS_DIR = Path("backups")
TOOLS_DIR   = Path(".tools")

NCNN_URL = (
    "https://github.com/xinntao/Real-ESRGAN/releases/download/"
    "v0.2.5.0/realesrgan-ncnn-vulkan-20220424-ubuntu.zip"
)
NCNN_BIN = TOOLS_DIR / "realesrgan-ncnn-vulkan"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def setup_ncnn() -> Path:
    """Descarga y prepara el binario Real-ESRGAN ncnn."""
    if NCNN_BIN.exists():
        return NCNN_BIN

    TOOLS_DIR.mkdir(exist_ok=True)
    zip_path = TOOLS_DIR / "realesrgan-ncnn.zip"

    log.info("Descargando Real-ESRGAN ncnn-vulkan...")
    r = requests.get(NCNN_URL, stream=True, timeout=120)
    r.raise_for_status()
    zip_path.write_bytes(r.content)

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(TOOLS_DIR)
    zip_path.unlink()

    NCNN_BIN.chmod(0o755)
    log.info(f"Binario listo: {NCNN_BIN}")
    return NCNN_BIN


def upscale_image_ncnn(img: Image.Image, ncnn_bin: Path,
                       outscale: int = 2) -> Image.Image:
    """Aplica Real-ESRGAN al imagen PIL usando el binario ncnn."""
    with tempfile.TemporaryDirectory() as tmp:
        inp = Path(tmp) / "input.png"
        out = Path(tmp) / "output.png"

        img.convert("RGB").save(str(inp), format="PNG")

        cmd = [
            str(ncnn_bin),
            "-i", str(inp),
            "-o", str(out),
            "-n", "realesrgan-x4plus",
            "-s", str(outscale),
            "-f", "png",
        ]
        log.info(f"    [ESRGAN ncnn] {img.size} outscale x{outscale}...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        if result.returncode != 0:
            log.warning(f"    [ESRGAN] stderr: {result.stderr[:300]}")
            raise RuntimeError(f"Real-ESRGAN falló (code {result.returncode})")

        upscaled = Image.open(str(out)).convert("RGB")
        log.info(f"    [ESRGAN] {img.size} → {upscaled.size}")
        return upscaled


def run_upscale(api: ShopifyAPI, vendor: str,
                product_ids: list, outscale: int = 2):
    slug        = vendor_slug(vendor)
    backup_root = BACKUPS_DIR / slug

    if not backup_root.exists():
        log.error(f"No existe backup para '{vendor}'.")
        sys.exit(1)

    ncnn_bin = setup_ncnn()

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

                upscaled = upscale_image_ncnn(img, ncnn_bin, outscale=outscale)
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
    parser.add_argument("--outscale",    type=int, default=2)
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
