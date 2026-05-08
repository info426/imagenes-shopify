#!/usr/bin/env python3
"""
Procesador de imágenes Farmina Vet Life para Shopify
======================================================
- Busca la imagen oficial de cada producto en farmina.com
- La descarga en la máxima resolución disponible
- La procesa: 2000×2000px, centrada, JPG alta calidad/bajo peso
- PNG sin fondo → fondo blanco
- Reemplaza la imagen en Shopify (borra la antigua, sube la nueva)
"""

import os
import re
import sys
import time
import base64
import logging
import argparse
import requests
from pathlib import Path
from io import BytesIO
from PIL import Image
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

SHOP_DOMAIN   = os.getenv("SHOP_DOMAIN",   "7ev1zx-eg.myshopify.com")
CLIENT_ID     = os.getenv("CLIENT_ID",     "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
VENDOR        = "Farmina Vet Life"
API_VERSION   = "2024-10"
TARGET_SIZE   = (2000, 2000)
JPEG_QUALITY  = 85
OUTPUT_DIR    = Path("imagenes_vet_life")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("vet_life_images.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ─── Autenticación ───────────────────────────────────────────────────────────

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
            params = {}
            url = None
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

# ─── Búsqueda en farmina.com ──────────────────────────────────────────────────

def _clean_title(title: str) -> str:
    """Extrae palabras clave del título eliminando el prefijo de la marca."""
    t = title.upper()
    for prefix in ["FARMINA VET LIFE ", "FARMINA VETLIFE ", "FARMINA "]:
        t = t.replace(prefix, "")
    # Quitar contenido entre paréntesis y unidades de peso
    t = re.sub(r"\(.*?\)", "", t)
    t = re.sub(r"\d+[\.,]?\d*\s*(KG|G|GR|L|ML)\b", "", t, flags=re.IGNORECASE)
    return t.strip().lower()

def _extract_images_from_page(html: str, base_url: str) -> list:
    """Extrae todas las URLs de imágenes de producto de una página HTML."""
    soup = BeautifulSoup(html, "html.parser")
    candidates = []

    # Buscar imágenes en elementos típicos de producto
    selectors = [
        "img.product__image", "img.wp-post-image",
        ".product-image img", ".woocommerce-product-gallery img",
        ".product img", "img[class*='product']",
        "figure img", ".entry-content img",
    ]
    for sel in selectors:
        for img in soup.select(sel):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            if src and any(ext in src.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                # Intentar obtener versión de mayor resolución
                srcset = img.get("srcset", "")
                if srcset:
                    parts = [p.strip() for p in srcset.split(",") if p.strip()]
                    best = parts[-1].split()[0] if parts else src
                    candidates.append(best)
                else:
                    candidates.append(src)

    # También buscar en JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            import json
            data = json.loads(script.string or "")
            if isinstance(data, dict):
                img = data.get("image") or data.get("image", {}).get("url", "")
                if img and isinstance(img, str):
                    candidates.append(img)
                elif isinstance(img, list):
                    candidates.extend(img)
        except Exception:
            pass

    # Normalizar URLs relativas
    from urllib.parse import urljoin
    return [urljoin(base_url, u) for u in dict.fromkeys(candidates) if u]

def search_farmina_vet_life_image(title: str, current_img_url: str) -> str | None:
    """
    Busca la imagen oficial del producto en farmina.com.
    Estrategias (en orden):
    1. Buscar por título en farmina.com/es (versión española)
    2. Buscar por título en farmina.com (internacional)
    3. Intentar URL directa construida desde el título
    """
    keywords = _clean_title(title)
    query    = keywords.replace(" ", "+")
    log.info(f"  Buscando en farmina.com: '{keywords}'")

    search_attempts = [
        f"https://www.farmina.com/es/?s={query}",
        f"https://www.farmina.com/?s={query}",
        f"https://www.farmina.com/es/buscar/?q={query}",
    ]

    for search_url in search_attempts:
        try:
            resp = requests.get(search_url, headers=HEADERS, timeout=20)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")

            # Buscar links a páginas de producto
            product_links = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                kws = keywords.split()
                if any(kw in href.lower() for kw in kws if len(kw) > 3):
                    product_links.append(href)

            # También buscar resultados de búsqueda directos
            for sel in [".product a", ".woocommerce-loop-product a",
                        "article a", ".entry-title a"]:
                for a in soup.select(sel):
                    href = a.get("href", "")
                    if href:
                        product_links.append(href)

            # Visitar cada página de producto y buscar imagen
            seen = set()
            for link in product_links[:5]:  # máximo 5 páginas de producto
                if link in seen:
                    continue
                seen.add(link)
                try:
                    from urllib.parse import urljoin
                    full_link = urljoin("https://www.farmina.com", link)
                    page = requests.get(full_link, headers=HEADERS, timeout=20)
                    if page.status_code != 200:
                        continue
                    images = _extract_images_from_page(page.text, full_link)
                    # Filtrar imágenes pequeñas/iconos por URL
                    images = [u for u in images if not any(
                        x in u.lower() for x in ["logo", "icon", "banner",
                                                   "sprite", "avatar"])]
                    if images:
                        log.info(f"  Imagen encontrada en: {full_link}")
                        return images[0]
                    time.sleep(0.5)
                except Exception as e:
                    log.debug(f"  Error visitando {link}: {e}")

        except Exception as e:
            log.debug(f"  Error en búsqueda {search_url}: {e}")
        time.sleep(1)

    log.warning(f"  No se encontró imagen en farmina.com para: {title}")
    return None

# ─── Procesamiento de imágenes ────────────────────────────────────────────────

def _has_transparency(img: Image.Image) -> bool:
    return img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info)

def _sample_bg_color(img: Image.Image) -> tuple:
    rgb = img.convert("RGB")
    w, h = rgb.size
    corners = [rgb.getpixel((0, 0)), rgb.getpixel((w-1, 0)),
               rgb.getpixel((0, h-1)), rgb.getpixel((w-1, h-1))]
    return tuple(sum(c[i] for c in corners) // 4 for i in range(3))

def process_image(img: Image.Image) -> Image.Image:
    """
    Redimensiona a 2000×2000, centrada.
    - Transparente → fondo blanco
    - Con fondo → conserva color de fondo original
    """
    transparent = _has_transparency(img)
    bg_color    = (255, 255, 255) if transparent else _sample_bg_color(img)
    img_rgb     = img.convert("RGBA") if transparent else img.convert("RGB")

    ratio = img_rgb.width / img_rgb.height
    if ratio > 1:
        new_w, new_h = TARGET_SIZE[0], int(TARGET_SIZE[0] / ratio)
    else:
        new_w, new_h = int(TARGET_SIZE[1] * ratio), TARGET_SIZE[1]

    resized    = img_rgb.resize((new_w, new_h), Image.LANCZOS)
    background = Image.new("RGB", TARGET_SIZE, bg_color)
    ox = (TARGET_SIZE[0] - new_w) // 2
    oy = (TARGET_SIZE[1] - new_h) // 2

    if transparent:
        background.paste(resized.convert("RGB"), (ox, oy), resized.split()[3])
    else:
        background.paste(resized, (ox, oy))

    return background

def to_b64_jpeg(img: Image.Image) -> str:
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY,
             optimize=True, progressive=True)
    return base64.b64encode(buf.getvalue()).decode()

def download_image(url: str) -> Image.Image:
    resp = requests.get(url, timeout=30, headers=HEADERS)
    resp.raise_for_status()
    return Image.open(BytesIO(resp.content))

# ─── Flujo principal ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-id", type=int, default=None,
                        help="Procesar solo este product ID (modo prueba)")
    args = parser.parse_args()

    if not CLIENT_ID or not CLIENT_SECRET:
        log.error("Faltan CLIENT_ID o CLIENT_SECRET")
        sys.exit(1)

    OUTPUT_DIR.mkdir(exist_ok=True)

    log.info("=" * 60)
    log.info("PROCESADOR FARMINA VET LIFE — SHOPIFY")
    log.info("=" * 60)

    token = get_token()
    api   = ShopifyAPI(token)

    if args.product_id:
        log.info(f"Modo prueba — producto ID: {args.product_id}")
        products = [api.get_product(args.product_id)]
    else:
        log.info(f"Obteniendo productos '{VENDOR}'...")
        products = api.get_products(VENDOR)
        log.info(f"Total: {len(products)} productos\n")

    stats = dict(total=len(products), actualizadas=0,
                 sin_imagen_web=0, errores=0)

    for i, product in enumerate(products, 1):
        pid   = product["id"]
        title = product["title"]
        log.info(f"\n[{i}/{len(products)}] {title}  (ID: {pid})")

        try:
            images = api.get_images(pid)
            if not images:
                log.warning("  Sin imágenes en Shopify — saltando")
                stats["sin_imagen_web"] += 1
                continue

            # Usar primera imagen actual como referencia de identificación
            current_img_url = images[0]["src"]

            # Buscar imagen oficial en farmina.com
            official_url = search_farmina_vet_life_image(title, current_img_url)

            if not official_url:
                log.warning(f"  No encontrada en farmina.com — saltando")
                stats["sin_imagen_web"] += 1
                continue

            # Descargar y procesar imagen oficial
            log.info(f"  Descargando imagen oficial: {official_url}")
            official_img = download_image(official_url)
            processed    = process_image(official_img)
            b64          = to_b64_jpeg(processed)
            fname        = f"vetlife_{pid}_oficial.jpg"
            processed.save(OUTPUT_DIR / fname, "JPEG",
                           quality=JPEG_QUALITY, optimize=True)
            log.info(f"  Tamaño final: {processed.size}, guardado: {fname}")

            # Reemplazar TODAS las imágenes actuales con la imagen oficial
            # Primero borrar las existentes, luego subir la nueva en posición 1
            for j, img_data in enumerate(images):
                api.delete_image(pid, img_data["id"])
                time.sleep(0.2)
                log.info(f"  Imagen {j+1}/{len(images)} borrada")

            api.create_image(pid, b64, fname, alt=title, position=1)
            log.info(f"  ✓ Imagen oficial subida a Shopify")
            stats["actualizadas"] += 1

        except Exception as exc:
            log.error(f"  ERROR: {exc}")
            stats["errores"] += 1

        time.sleep(1)

    log.info("\n" + "=" * 60)
    log.info("RESUMEN FINAL")
    log.info("=" * 60)
    log.info(f"  Productos procesados         : {stats['total']}")
    log.info(f"  Imágenes actualizadas        : {stats['actualizadas']}")
    log.info(f"  No encontradas en farmina.com: {stats['sin_imagen_web']}")
    log.info(f"  Errores                      : {stats['errores']}")

if __name__ == "__main__":
    main()
