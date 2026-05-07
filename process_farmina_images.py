#!/usr/bin/env python3
"""
Procesador de imágenes Farmina ND para Shopify
================================================
- Obtiene access token via client credentials (Dev Dashboard)
- Busca todos los productos del vendor configurado
- Descarga, procesa y sube las imágenes que no cumplan:
    * 2000x2000 px
    * Fondo blanco
    * JPEG alta calidad / bajo peso
- Para productos sin imagen busca en farmina.com
"""

import os
import re
import sys
import json
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

# ─── Configuración ───────────────────────────────────────────────────────────

SHOP_DOMAIN   = os.getenv("SHOP_DOMAIN",   "7ev1zx-eg.myshopify.com")
CLIENT_ID     = os.getenv("CLIENT_ID",     "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
VENDOR_FILTER = os.getenv("VENDOR_FILTER", "Farmina ND")

API_VERSION  = "2024-10"
TARGET_SIZE  = (2000, 2000)
JPEG_QUALITY = 85          # relación óptima calidad/peso
OUTPUT_DIR   = Path("imagenes_procesadas")

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("farmina_images.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ─── Autenticación ───────────────────────────────────────────────────────────

def get_access_token() -> str:
    url = f"https://{SHOP_DOMAIN}/admin/oauth/access_token"
    resp = requests.post(url, data={
        "grant_type":    "client_credentials",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }, timeout=15)
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise ValueError(f"Respuesta inesperada al obtener token: {resp.text}")
    log.info("Access token obtenido correctamente")
    return token

# ─── API Shopify ──────────────────────────────────────────────────────────────

class ShopifyAPI:
    def __init__(self, token: str):
        self.base = f"https://{SHOP_DOMAIN}/admin/api/{API_VERSION}"
        self.headers = {
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict = None):
        resp = requests.get(f"{self.base}{path}", headers=self.headers,
                            params=params, timeout=30)
        resp.raise_for_status()
        return resp

    def _put(self, path: str, payload: dict):
        resp = requests.put(f"{self.base}{path}", headers=self.headers,
                            json=payload, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: dict):
        resp = requests.post(f"{self.base}{path}", headers=self.headers,
                             json=payload, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def get_products(self, vendor: str = None) -> list:
        products, params = [], {"limit": 250}
        if vendor:
            params["vendor"] = vendor
        url = f"{self.base}/products.json"
        while url:
            resp = requests.get(url, headers=self.headers, params=params, timeout=30)
            resp.raise_for_status()
            batch = resp.json().get("products", [])
            products.extend(batch)
            log.info(f"  Productos cargados: {len(products)}")
            params = {}
            url = None
            link = resp.headers.get("Link", "")
            if 'rel="next"' in link:
                for part in link.split(","):
                    if 'rel="next"' in part:
                        url = part.strip().split(";")[0].strip("<>")
        return products

    def get_images(self, product_id: int) -> list:
        resp = self._get(f"/products/{product_id}/images.json")
        return resp.json().get("images", [])

    def delete_image(self, product_id: int, image_id: int) -> None:
        resp = requests.delete(
            f"{self.base}/products/{product_id}/images/{image_id}.json",
            headers=self.headers, timeout=30,
        )
        resp.raise_for_status()

    def create_image(self, product_id: int, b64: str,
                     filename: str, alt: str = "", position: int = None) -> dict:
        payload = {"attachment": b64, "filename": filename, "alt": alt}
        if position is not None:
            payload["position"] = position
        return self._post(
            f"/products/{product_id}/images.json",
            {"image": payload},
        )

# ─── Procesamiento de imágenes ────────────────────────────────────────────────

def _has_transparency(img: Image.Image) -> bool:
    return img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)

def _sample_bg_color(img: Image.Image) -> tuple:
    """Muestrea el color de fondo desde las esquinas de la imagen."""
    rgb = img.convert("RGB")
    w, h = rgb.size
    corners = [
        rgb.getpixel((0, 0)),
        rgb.getpixel((w - 1, 0)),
        rgb.getpixel((0, h - 1)),
        rgb.getpixel((w - 1, h - 1)),
    ]
    r = sum(c[0] for c in corners) // 4
    g = sum(c[1] for c in corners) // 4
    b = sum(c[2] for c in corners) // 4
    return (r, g, b)

def check_issues(img: Image.Image) -> dict:
    issues = {}
    if img.size != TARGET_SIZE:
        issues["tamaño"] = f"{img.size[0]}×{img.size[1]} → 2000×2000"
    if img.format != "JPEG":
        issues["formato"] = f"{img.format or '?'} → JPEG"
    return issues

def process_image(img: Image.Image) -> Image.Image:
    """
    Redimensiona a 2000×2000 y convierte a RGB/JPG.
    - PNG con transparencia → fondo blanco
    - Resto → fondo del color original de la imagen
    """
    transparent = _has_transparency(img)
    bg_color = (255, 255, 255) if transparent else _sample_bg_color(img)

    img_rgba = img.convert("RGBA") if transparent else img.convert("RGB")

    ratio = img_rgba.width / img_rgba.height
    if ratio > 1:
        new_w, new_h = TARGET_SIZE[0], int(TARGET_SIZE[0] / ratio)
    else:
        new_w, new_h = int(TARGET_SIZE[1] * ratio), TARGET_SIZE[1]

    resized    = img_rgba.resize((new_w, new_h), Image.LANCZOS)
    background = Image.new("RGB", TARGET_SIZE, bg_color)
    ox = (TARGET_SIZE[0] - new_w) // 2
    oy = (TARGET_SIZE[1] - new_h) // 2

    if transparent:
        background.paste(resized.convert("RGB"), (ox, oy),
                         resized.split()[3])  # usa canal alpha como máscara
    else:
        background.paste(resized, (ox, oy))

    return background

def to_b64_jpeg(img: Image.Image) -> str:
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY,
             optimize=True, progressive=True)
    return base64.b64encode(buf.getvalue()).decode()

def download_image(url: str) -> Image.Image:
    resp = requests.get(url, timeout=30,
                        headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return Image.open(BytesIO(resp.content))

# ─── Búsqueda en farmina.com ──────────────────────────────────────────────────

FARMINA_SEARCH_URLS = [
    "https://www.farmina.com/es/",
    "https://www.farmina.com/",
]

def search_farmina_image(title: str) -> str | None:
    query = re.sub(r"[^\w\s]", "", title).strip().replace(" ", "+")
    for base in FARMINA_SEARCH_URLS:
        try:
            url = f"{base}?s={query}"
            resp = requests.get(url, timeout=15,
                                headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                continue
            # Busca la primera imagen de producto mencionada en el HTML
            patterns = [
                r'<img[^>]+src=["\']([^"\']+(?:jpg|jpeg|png|webp))["\'][^>]+class=["\'][^"\']*product',
                r'<img[^>]+class=["\'][^"\']*product[^"\']*["\'][^>]+src=["\']([^"\']+(?:jpg|jpeg|png|webp))["\']',
                r'"url":"(https://[^"]+(?:jpg|jpeg|png|webp))"',
            ]
            for pat in patterns:
                matches = re.findall(pat, resp.text, re.IGNORECASE)
                if matches:
                    log.info(f"  Imagen encontrada en farmina.com: {matches[0]}")
                    return matches[0]
        except Exception as exc:
            log.debug(f"  Error buscando en {base}: {exc}")
    return None

# ─── Flujo principal ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-id", type=int, default=None,
                        help="Procesar solo este product ID (modo prueba)")
    args = parser.parse_args()

    if not CLIENT_ID or not CLIENT_SECRET:
        log.error("Faltan CLIENT_ID o CLIENT_SECRET. Revisa el archivo .env")
        sys.exit(1)

    OUTPUT_DIR.mkdir(exist_ok=True)

    log.info("=" * 60)
    log.info("PROCESADOR DE IMÁGENES FARMINA ND — SHOPIFY")
    log.info("=" * 60)
    log.info(f"Tienda : {SHOP_DOMAIN}")

    token = get_access_token()
    api   = ShopifyAPI(token)

    if args.product_id:
        log.info(f"Modo prueba — producto ID: {args.product_id}")
        # Obtener solo el producto solicitado directamente
        resp = requests.get(
            f"https://{SHOP_DOMAIN}/admin/api/{API_VERSION}/products/{args.product_id}.json",
            headers={"X-Shopify-Access-Token": token},
            timeout=30,
        )
        resp.raise_for_status()
        products = [resp.json()["product"]]
    else:
        log.info(f"Vendor : {VENDOR_FILTER}")
        log.info(f"\nObteniendo productos con vendor='{VENDOR_FILTER}'...")
        products = api.get_products(vendor=VENDOR_FILTER)
        log.info(f"Total productos encontrados: {len(products)}\n")

    if not products:
        log.warning("Sin resultados.")
        return

    stats = dict(total=len(products), actualizadas=0,
                 sin_cambios=0, nuevas_web=0,
                 sin_imagen_no_encontrada=0, errores=0)

    for i, product in enumerate(products, 1):
        pid   = product["id"]
        title = product["title"]
        log.info(f"[{i}/{len(products)}] {title}  (ID: {pid})")

        try:
            images = api.get_images(pid)

            # ── Producto sin imágenes ──────────────────────────────────────
            if not images:
                log.info("  Sin imágenes — buscando en farmina.com...")
                img_url = search_farmina_image(title)
                if img_url:
                    img = download_image(img_url)
                    processed = process_image(img)
                    b64 = to_b64_jpeg(processed)
                    fname = f"farmina_{pid}_1.jpg"
                    processed.save(OUTPUT_DIR / fname, "JPEG",
                                   quality=JPEG_QUALITY, optimize=True)
                    api.create_image(pid, b64, fname, alt=title)
                    log.info(f"  ✓ Imagen subida desde farmina.com")
                    stats["nuevas_web"] += 1
                else:
                    log.warning("  No se encontró imagen en farmina.com")
                    stats["sin_imagen_no_encontrada"] += 1
                continue

            # ── Procesar imágenes existentes ──────────────────────────────
            for j, image in enumerate(images, 1):
                iid      = image["id"]
                src      = image["src"]
                alt      = image.get("alt") or ""
                position = image.get("position", j)
                log.info(f"  Imagen {j}/{len(images)}: {src}")

                img    = download_image(src)
                issues = check_issues(img)

                if not issues:
                    log.info("  ✓ Ya cumple todos los requisitos — omitida")
                    stats["sin_cambios"] += 1
                    continue

                log.info(f"  Procesando → {issues}")
                processed = process_image(img)
                b64   = to_b64_jpeg(processed)
                fname = f"farmina_{pid}_{iid}.jpg"
                processed.save(OUTPUT_DIR / fname, "JPEG",
                               quality=JPEG_QUALITY, optimize=True)

                # Borrar imagen antigua y crear nueva en JPG para que la URL
                # del CDN refleje el formato correcto (.jpg)
                api.delete_image(pid, iid)
                time.sleep(0.3)
                api.create_image(pid, b64, fname, alt=alt, position=position)
                log.info(f"  ✓ Imagen reemplazada como JPG en Shopify")
                stats["actualizadas"] += 1
                time.sleep(0.5)

        except Exception as exc:
            log.error(f"  ERROR: {exc}")
            stats["errores"] += 1

    # ── Resumen ───────────────────────────────────────────────────────────────
    log.info("\n" + "=" * 60)
    log.info("RESUMEN FINAL")
    log.info("=" * 60)
    log.info(f"  Productos procesados         : {stats['total']}")
    log.info(f"  Imágenes actualizadas        : {stats['actualizadas']}")
    log.info(f"  Imágenes sin cambios         : {stats['sin_cambios']}")
    log.info(f"  Imágenes nuevas (farmina.com): {stats['nuevas_web']}")
    log.info(f"  Sin imagen (no encontrada)   : {stats['sin_imagen_no_encontrada']}")
    log.info(f"  Errores                      : {stats['errores']}")
    log.info(f"\n  Copias locales → {OUTPUT_DIR}/")
    log.info(f"  Log completo   → farmina_images.log")


if __name__ == "__main__":
    main()
