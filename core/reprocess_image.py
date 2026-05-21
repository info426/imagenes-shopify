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
from core.image_utils import (TARGET_SIZE, process_image, process_image_webp_only,
                              to_webp_b64, to_webp_srgb_b64)
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
                          new_img: Image.Image, title: str, slug: str,
                          srgb: bool = False):
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
    encoder = to_webp_srgb_b64 if srgb else to_webp_b64
    api.upload_image(product_id, encoder(new_img), fname, alt=alt, position=position)
    log.info(f"  ✓ Subida: {fname} en posición {position} {'[sRGB]' if srgb else ''}")


# ─── Modo escaneo de marca ─────────────────────────────────────────────────────

def is_optimized(img_meta: dict) -> tuple:
    """
    Decide si una imagen YA cumple el estándar, usando solo los metadatos que
    devuelve Shopify (sin descargarla): formato WebP y dimensiones 2000×2000.
    Las imágenes subidas a mano (jpg/png u otro tamaño) no cumplen → reprocesar.
    Devuelve (cumple, descripción).
    """
    src = (img_meta.get("src") or "").split("?")[0].lower()
    w   = img_meta.get("width") or 0
    h   = img_meta.get("height") or 0
    is_webp    = src.endswith(".webp")
    right_size = (w, h) == TARGET_SIZE
    fmt = src.rsplit(".", 1)[-1] if "." in src.rsplit("/", 1)[-1] else "?"
    return (is_webp and right_size), f"{fmt} {w}×{h}"


def reprocess_in_place(api: ShopifyAPI, pid: int, img_meta: dict, title: str,
                       pipeline: str, force_padding: bool | None, srgb: bool):
    """
    Descarga la imagen viva de Shopify (el original subido a mano), la procesa
    y la sustituye conservando su posición y su alt. Borra por ID de imagen y
    re-sube en la misma posición → el resto de imágenes no se reordena.
    """
    pos = img_meta.get("position")
    alt = img_meta.get("alt") or title
    url = img_meta["src"].split("?")[0]
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    img = Image.open(BytesIO(r.content))
    log.info(f"    cargada {img.mode} {img.size} ← {url.split('/')[-1]}")

    if pipeline == "webp_only":
        processed = process_image_webp_only(img)
    else:
        processed = process_image(img, force_padding=force_padding)

    api.delete_image(pid, img_meta["id"])
    time.sleep(0.3)
    fname   = f"{title_slug(title)}_{pos}.webp"
    encoder = to_webp_srgb_b64 if srgb else to_webp_b64
    api.upload_image(pid, encoder(processed), fname, alt=alt, position=pos)
    log.info(f"    ✓ reprocesada y subida [{fname}] en posición {pos}")


def run_scan_brand(api: ShopifyAPI, vendor: str, pipeline: str,
                   force_padding: bool | None, srgb: bool):
    """
    Recorre todos los productos del vendor y, producto por producto, reprocesa
    únicamente las imágenes que no cumplen el estándar (WebP 2000×2000),
    omitiendo las ya optimizadas y respetando el orden existente.
    """
    products = api.get_products(vendor)
    log.info(f"Total productos de '{vendor}': {len(products)}")

    stats = dict(productos=len(products), reprocesadas=0, omitidas=0,
                 sin_imagenes=0, errores=0)

    for i, product in enumerate(products, 1):
        pid    = product["id"]
        title  = product["title"]
        images = sorted(api.get_images(pid), key=lambda x: x.get("position", 999))
        log.info(f"\n[{i}/{len(products)}] {title}  (ID {pid}) — {len(images)} imágenes")

        if not images:
            stats["sin_imagenes"] += 1
            continue

        # Posiciones capturadas al inicio. Como cada sustitución borra+re-sube en
        # la misma posición (operación neutra para el resto), las posiciones de
        # las imágenes pendientes siguen siendo válidas en iteraciones posteriores.
        for img_meta in images:
            pos = img_meta.get("position")
            ok, desc = is_optimized(img_meta)
            if ok:
                log.info(f"  pos {pos}: ya optimizada ({desc}) — omitida")
                stats["omitidas"] += 1
                continue
            log.info(f"  pos {pos}: NO optimizada ({desc}) — reprocesando")
            try:
                reprocess_in_place(api, pid, img_meta, title,
                                   pipeline, force_padding, srgb)
                stats["reprocesadas"] += 1
            except Exception as e:
                log.warning(f"  pos {pos}: error — {e}")
                stats["errores"] += 1
            time.sleep(0.5)

    log.info("\n" + "=" * 60)
    log.info("RESUMEN")
    log.info("=" * 60)
    for k, v in stats.items():
        log.info(f"  {k:<14}: {v}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor",        required=True)
    parser.add_argument("--product-id",    type=int, default=None,
                        help="ID del producto. Omitir → escanea TODA la marca")
    parser.add_argument("--position",      type=int, default=None,
                        help="Posición de la imagen (1-based). Omitir con --all-positions")
    parser.add_argument("--all-positions", action="store_true",
                        help="Reprocesar todas las imágenes del producto")
    parser.add_argument("--force-padding", choices=["auto", "true", "false"], default="auto")
    parser.add_argument("--pipeline",      choices=["standard", "webp_only"], default="standard")
    parser.add_argument("--srgb",          action="store_true",
                        help="Embeber perfil de color sRGB en el WebP")
    args = parser.parse_args()

    force_padding: bool | None = {"auto": None, "true": True, "false": False}[args.force_padding]

    log.info("=" * 60)
    log.info(f"  Vendor    : {args.vendor}")
    log.info(f"  Modo      : {'ESCANEO MARCA' if args.product_id is None else 'individual'}")
    log.info(f"  Pipeline  : {args.pipeline}")
    log.info(f"  Padding   : {args.force_padding}")
    log.info(f"  sRGB      : {'sí' if args.srgb else 'no'}")
    log.info("=" * 60)

    token = get_token()
    api   = ShopifyAPI(token)
    slug  = vendor_slug(args.vendor)

    # Sin product_id → escaneo de marca: reprocesa solo las imágenes no optimizadas.
    if args.product_id is None:
        run_scan_brand(api, args.vendor, args.pipeline, force_padding, args.srgb)
        log.info("\n✓ Completado")
        return

    if not args.all_positions and args.position is None:
        parser.error("Especifica --position N o --all-positions "
                     "(o deja --product-id vacío para escanear toda la marca)")

    log.info(f"  Producto  : {args.product_id}")
    log.info(f"  Posición  : {'todas' if args.all_positions else args.position}")

    product = api.get_product(args.product_id)
    title   = product["title"]
    log.info(f"\n[{args.product_id}] {title}")

    if args.all_positions:
        # Obtener posiciones desde backup o desde Shopify
        folder = BACKUPS_DIR / slug / str(args.product_id)
        if folder.exists():
            positions = sorted([
                int(p.stem.split("_")[1])
                for p in folder.iterdir()
                if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
                and p.stem.startswith("img_")
            ])
        else:
            images = api.get_images(args.product_id)
            positions = [img["position"] for img in sorted(images, key=lambda x: x.get("position", 999))]
        log.info(f"  Posiciones a procesar: {positions}")
    else:
        positions = [args.position]

    for pos in positions:
        log.info(f"\n  — Posición {pos} —")
        img = load_image_from_backup(slug, args.product_id, pos)
        if img is None:
            img = load_image_from_shopify(api, args.product_id, pos)
        if img is None:
            log.warning(f"  No se pudo obtener imagen en posición {pos} — saltando")
            continue

        log.info(f"  Imagen cargada: {img.mode} {img.size}")

        if args.pipeline == "webp_only":
            processed = process_image_webp_only(img)
        else:
            processed = process_image(img, force_padding=force_padding)

        replace_single_image(api, args.product_id, pos, processed, title, slug, srgb=args.srgb)
        time.sleep(0.5)

    log.info("\n✓ Completado")


if __name__ == "__main__":
    main()
