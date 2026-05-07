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
VENDOR_FILTER = os.getenv("VENDOR_FILTER", "Farmina")

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

    def update_image(self, product_id: int, image_id: int,
                     b64: str, filename: str) -> dict:
        return self._put(
            f"/products/{product_id}/images/{image_id}.json",
            {"image": {"id": image_id, "attachment": b64, "filename": filename}},
        )

    def create_image(self, product_id: int, b64: str,
                     filename: str, alt: str = "") -> dict:
        return self._post(
            f"/products/{product_id}/images.json",
            {"image": {"attachment": b64, "filename": filename, "alt": alt}},
        )

# ─── Procesamiento de imágenes ────────────────────────────────────────────────

def check_issues(img: Image.Image) -> dict:
    issues = {}
    if img.size != TARGET_SIZE:
        issues["tamaño"] = f"{img.size[0]}×{img.size[1]} → 2000×2000"
    if img.format != "JPEG":
        issues["formato"] = f"{img.format or '?'} → JPEG"
    return issues

def _sample_bg_color(img: Image.Image) -> tuple:
    """Muestrea el color de fondo dominante desde las esquinas."""
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

def process_image(img: Image.Image) -> Image.Image:
    """Redimensiona a 2000×2000 manteniendo proporciones y fondo original."""
    bg_color = _sample_bg_color(img)
    img_rgb  = img.convert("RGB")
    ratio    = img_rgb.width / img_rgb.height
    if ratio > 1:
        new_w, new_h = TARGET_SIZE[0], int(TARGET_SIZE[0] / ratio)
    else:
        new_w, new_h = int(TARGET_SIZE[1] * ratio), TARGET_SIZE[1]
    resized    = img_rgb.resize((new_w, new_h), Image.LANCZOS)
    background = Image.new("RGB", TARGET_SIZE, bg_color)
    ox = (TARGET_SIZE[0] - new_w) // 2
    oy = (TARGET_SIZE[1] - new_h) // 2
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
    if not CLIENT_ID or not CLIENT_SECRET:
        log.error("Faltan CLIENT_ID o CLIENT_SECRET. Revisa el archivo .env")
        sys.exit(1)

    OUTPUT_DIR.mkdir(exist_ok=True)

    log.info("=" * 60)
    log.info("PROCESADOR DE IMÁGENES FARMINA ND — SHOPIFY")
    log.info("=" * 60)
    log.info(f"Tienda : {SHOP_DOMAIN}")
    log.info(f"Vendor : {VENDOR_FILTER}")

    token = get_access_token()
    api   = ShopifyAPI(token)

    log.info(f"\nObteniendo productos con vendor='{VENDOR_FILTER}'...")
    products = api.get_products(vendor=VENDOR_FILTER)
    log.info(f"Total productos encontrados: {len(products)}\n")

    if not products:
        log.warning("Sin resultados. Verifica VENDOR_FILTER en .env.")
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
                iid = image["id"]
                src = image["src"]
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
                api.update_image(pid, iid, b64, fname)
                log.info(f"  ✓ Imagen actualizada en Shopify")
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
