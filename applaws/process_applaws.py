#!/usr/bin/env python3
"""
Procesador de imágenes Applaws para Shopify
============================================
Las imágenes ya están en Shopify con alta calidad.
El script descarga cada imagen, la procesa (2000×2000 WebP,
fondo blanco) y la re-sube reemplazando la original.

Regla de padding:
  - Fondo blanco (o transparente) → 5% padding en cada lado
  - Ilustraciones con fondo de color → sin padding, rellena 2000×2000

Uso:
  python3 applaws/process_applaws.py                         # todos los productos
  python3 applaws/process_applaws.py --product-id 123456     # modo prueba
  python3 applaws/process_applaws.py --only-ids 123,456,789  # lista específica
  PRODUCT_IDS=123,456 python3 applaws/process_applaws.py     # via env var
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
from PIL import Image
from dotenv import load_dotenv

load_dotenv()

SHOP_DOMAIN   = os.getenv("SHOP_DOMAIN",   "7ev1zx-eg.myshopify.com")
CLIENT_ID     = os.getenv("CLIENT_ID",     "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
VENDOR        = "Applaws"
API_VERSION   = "2024-10"
TARGET_SIZE   = (2000, 2000)
WEBP_QUALITY  = 90        # alta calidad WebP sin pérdida visual apreciable
OUTPUT_DIR    = Path("imagenes_applaws")
PADDING       = 0.05      # 5% en cada lado (solo fondos blancos)
WHITE_THRESH  = 245       # umbral para considerar un píxel como blanco
WHITE_MIN_FRAC = 0.75     # fracción mínima de puntos blancos para considerar fondo blanco

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
}

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("applaws_images.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ─── Autenticación Shopify ────────────────────────────────────────────────────

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

# ─── Shopify API ──────────────────────────────────────────────────────────────

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

# ─── Procesamiento de imágenes ────────────────────────────────────────────────

def _has_transparency(img: Image.Image) -> bool:
    return img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info)


def _is_white_background(img: Image.Image) -> bool:
    """
    Muestrea esquinas y bordes para detectar si el fondo es blanco.
    Retorna True si al menos WHITE_MIN_FRAC de los puntos muestreados
    son >= WHITE_THRESH en los tres canales RGB.
    """
    rgb = img.convert("RGB")
    w, h = rgb.size
    samples = [
        (0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),  # esquinas
        (w // 2, 0), (w // 2, h - 1),                      # bordes superior/inferior
        (0, h // 2), (w - 1, h // 2),                      # bordes izquierdo/derecho
    ]
    white = sum(
        1 for x, y in samples
        if all(c >= WHITE_THRESH for c in rgb.getpixel((x, y)))
    )
    return white / len(samples) >= WHITE_MIN_FRAC


def process_image(img: Image.Image) -> Image.Image:
    """
    Redimensiona a 2000×2000 con fondo blanco.
    - Fondo blanco o transparente: aplica 5% de padding en cada lado.
    - Ilustraciones con fondo de color: rellena los 2000×2000 sin margen.
    Usa LANCZOS para preservar la resolución al redimensionar.
    """
    transparent = _has_transparency(img)
    img_conv = img.convert("RGBA") if transparent else img.convert("RGB")

    use_padding = transparent or _is_white_background(img)
    label = "fondo blanco → padding 5%" if use_padding else "ilustración → sin padding"
    log.info(f"    [{label}]")

    if use_padding:
        max_w = int(TARGET_SIZE[0] * (1 - 2 * PADDING))  # 1900 px
        max_h = int(TARGET_SIZE[1] * (1 - 2 * PADDING))  # 1900 px
    else:
        max_w, max_h = TARGET_SIZE  # 2000 px

    ratio = img_conv.width / img_conv.height
    if ratio > 1:
        new_w, new_h = max_w, int(max_w / ratio)
    else:
        new_w, new_h = int(max_h * ratio), max_h

    resized    = img_conv.resize((new_w, new_h), Image.LANCZOS)
    background = Image.new("RGB", TARGET_SIZE, (255, 255, 255))
    ox = (TARGET_SIZE[0] - new_w) // 2
    oy = (TARGET_SIZE[1] - new_h) // 2

    if transparent:
        background.paste(resized.convert("RGB"), (ox, oy), resized.split()[3])
    else:
        background.paste(resized, (ox, oy))

    return background


def to_b64_webp(img: Image.Image) -> str:
    buf = BytesIO()
    img.save(buf, format="WEBP", quality=WEBP_QUALITY, method=6)
    return base64.b64encode(buf.getvalue()).decode()


def download_image(url: str) -> Image.Image:
    # Eliminar parámetros de CDN para obtener la imagen original sin recortar
    clean_url = url.split("?")[0]
    resp = requests.get(clean_url, timeout=60, headers=HEADERS)
    resp.raise_for_status()
    return Image.open(BytesIO(resp.content))

# ─── Flujo principal ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Procesa imágenes Applaws en Shopify: 2000×2000 WebP, fondo blanco")
    parser.add_argument("--product-id", type=int, default=None,
                        help="Procesar solo este product ID (modo prueba)")
    parser.add_argument("--only-ids", type=str, default=None,
                        help="Lista de IDs separados por coma (ej: 123,456)")
    args = parser.parse_args()

    if not CLIENT_ID or not CLIENT_SECRET:
        log.error("Faltan CLIENT_ID o CLIENT_SECRET. Revisa el archivo .env")
        sys.exit(1)

    OUTPUT_DIR.mkdir(exist_ok=True)
    Path("resultados").mkdir(exist_ok=True)

    log.info("=" * 60)
    log.info("PROCESADOR APPLAWS — SHOPIFY")
    log.info("=" * 60)
    log.info(f"Tienda  : {SHOP_DOMAIN}")
    log.info(f"Vendor  : {VENDOR}")
    log.info(f"Formato : WebP calidad {WEBP_QUALITY}, 2000×2000")
    log.info(f"Padding : {int(PADDING*100)}% solo en imágenes con fondo blanco")

    token = get_token()
    api   = ShopifyAPI(token)

    only_ids_raw = args.only_ids or os.getenv("PRODUCT_IDS", "")
    only_ids: set[int] = set()
    if only_ids_raw:
        only_ids = {int(x.strip()) for x in only_ids_raw.split(",") if x.strip()}
        log.info(f"Modo re-proceso — {len(only_ids)} IDs específicos")

    if args.product_id:
        log.info(f"Modo prueba — producto ID: {args.product_id}")
        products = [api.get_product(args.product_id)]
    elif only_ids:
        log.info(f"Obteniendo {len(only_ids)} productos específicos...")
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
            images = api.get_images(pid)
            if not images:
                log.warning("  Sin imágenes en Shopify — saltando")
                stats["sin_imagen"] += 1
                continue

            log.info(f"  {len(images)} imagen(es) en Shopify")

            # Descargar y procesar todas las imágenes del producto
            processed_images = []
            for img_data in images:
                src = img_data["src"]
                alt = img_data.get("alt") or title
                try:
                    original  = download_image(src)
                    processed = process_image(original)
                    processed_images.append({
                        "processed": processed,
                        "alt":       alt,
                        "position":  img_data.get("position", len(processed_images) + 1),
                    })
                    log.info(f"  ✓ Procesada: {src.split('/')[-1].split('?')[0]}")
                except Exception as exc:
                    log.warning(f"  No se pudo procesar {src}: {exc}")

            if not processed_images:
                log.warning("  Ninguna imagen procesada — saltando")
                stats["errores"] += 1
                continue

            # Eliminar todas las imágenes antiguas
            for img_data in images:
                api.delete_image(pid, img_data["id"])
                time.sleep(0.2)
            log.info(f"  {len(images)} imagen(es) antigua(s) eliminada(s)")

            # Subir imágenes procesadas en formato WebP
            for pos, item in enumerate(processed_images, 1):
                b64   = to_b64_webp(item["processed"])
                fname = f"applaws_{pid}_{pos}.webp"
                item["processed"].save(
                    OUTPUT_DIR / fname, format="WEBP",
                    quality=WEBP_QUALITY, method=6,
                )
                api.create_image(pid, b64, fname, alt=item["alt"], position=pos)
                log.info(f"  ✓ Imagen {pos}/{len(processed_images)} subida → {fname}")
                time.sleep(0.5)

            stats["actualizadas"] += 1
            log.info(f"  Producto completado")

        except Exception as exc:
            log.error(f"  ERROR: {exc}")
            stats["errores"] += 1

        time.sleep(1)

    log.info("\n" + "=" * 60)
    log.info("RESUMEN FINAL")
    log.info("=" * 60)
    log.info(f"  Productos procesados  : {stats['total']}")
    log.info(f"  Imágenes actualizadas : {stats['actualizadas']}")
    log.info(f"  Sin imagen en Shopify : {stats['sin_imagen']}")
    log.info(f"  Errores               : {stats['errores']}")
    log.info(f"  Copias locales        → {OUTPUT_DIR}/")
    log.info(f"  Log completo          → applaws_images.log")


if __name__ == "__main__":
    main()
