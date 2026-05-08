#!/usr/bin/env python3
"""
Procesador de imágenes Farmina Vet Life para Shopify
======================================================
1. Playwright carga las categorías DOG + CAT de farmina.com
2. Extrae el catálogo de productos con sus imágenes (/fotoprodotti/)
3. Para cada producto Shopify, busca el mejor match por nombre
4. Descarga, procesa (2000×2000 JPG, fondo blanco) y sube a Shopify
"""

import os
import re
import sys
import time
import json
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
CATALOG_FILE  = Path("resultados/farmina_catalog.json")

FARMINA_BASE = "https://www.farmina.com"
CATEGORY_URLS = {
    "dog": f"{FARMINA_BASE}/es/alimento-para-perros/8-farmina-vet-life.html",
    "cat": f"{FARMINA_BASE}/es/alimento-para-gatos/14-farmina-vet-life.html",
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("vet_life_images.log", encoding="utf-8"),
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

# ─── Catálogo farmina.com con Playwright ──────────────────────────────────────

def _parse_category_html(html: str, species: str) -> list:
    """Extrae productos de la categoría renderizada por Playwright."""
    soup = BeautifulSoup(html, "html.parser")
    entries = []
    seen_ids = set()

    for hoverbox in soup.find_all("div", class_="hoverbox"):
        onclick = hoverbox.get("onclick", "")
        url_match = re.search(r"location\.href='([^']+)'", onclick)
        if not url_match:
            continue
        product_url = url_match.group(1)

        slug_match = re.search(r'/(\d+)-([^/]+)\.html', product_url)
        if not slug_match:
            continue
        product_id = slug_match.group(1)
        if product_id in seen_ids:
            continue
        seen_ids.add(product_id)
        slug = slug_match.group(2)

        name_el = hoverbox.find("div", class_="es-product-line-title")
        name = name_el.get_text(strip=True) if name_el else slug.replace("-", " ")

        img_el = hoverbox.find("img")
        img_src = img_el.get("src", "") if img_el else ""
        if not img_src.startswith("http"):
            img_src = FARMINA_BASE + img_src

        if "fotoprodotti" not in img_src:
            continue

        entries.append({
            "id": product_id,
            "name": name,
            "slug": slug,
            "url": product_url,
            "image_url": img_src,
            "species": species,
        })

    return entries


def build_farmina_catalog() -> list:
    """Carga el catálogo desde caché o lo construye con Playwright."""
    if CATALOG_FILE.exists():
        catalog = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
        log.info(f"Catálogo cargado desde caché: {len(catalog)} productos")
        return catalog

    log.info("Construyendo catálogo con Playwright...")
    from playwright.sync_api import sync_playwright

    catalog = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="es-ES",
            extra_http_headers={"Accept-Language": "es-ES,es;q=0.9"},
        )
        page = ctx.new_page()

        for species, url in CATEGORY_URLS.items():
            log.info(f"  Cargando categoría {species.upper()}: {url}")
            page.goto(url, timeout=40000, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            html = page.content()
            entries = _parse_category_html(html, species)
            log.info(f"  {species.upper()}: {len(entries)} productos encontrados")
            for e in entries:
                log.info(f"    [{e['id']}] {e['name']} → {e['image_url'].split('/')[-1]}")
            catalog.extend(entries)

        browser.close()

    CATALOG_FILE.parent.mkdir(exist_ok=True)
    CATALOG_FILE.write_text(json.dumps(catalog, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    log.info(f"Catálogo guardado: {len(catalog)} productos → {CATALOG_FILE}")
    return catalog

# ─── Matching Shopify ↔ farmina.com ──────────────────────────────────────────

_STOPWORDS = {"the", "and", "for", "con", "para", "del", "los", "las",
              "una", "que", "más", "wet", "dry", "food", "alimento"}
_IGNORE_TOKENS = {"vetlife", "vet", "life", "farmina", "canine", "feline",
                  "300g", "85g", "150g", "2kg", "3kg", "web", "sito", "amp"}


def _tokenize(text: str) -> set:
    text = re.sub(r'@\w+', '', text)
    text = re.sub(r'\d+[\.,]?\d*\s*(kg|g|gr|l|ml|tab)\b', '', text,
                  flags=re.IGNORECASE)
    text = re.sub(r'\(.*?\)', '', text)
    tokens = set(re.split(r'[\s\-_&+•\.]+', text.lower()))
    return tokens - _STOPWORDS - _IGNORE_TOKENS - {''}


def _clean_shopify_title(title: str) -> str:
    t = title.upper()
    for prefix in ["FARMINA VET LIFE ", "FARMINA VETLIFE ", "FARMINA "]:
        t = t.replace(prefix, "")
    return t


def _score(shopify_title: str, entry: dict) -> float:
    shopify_tokens = _tokenize(_clean_shopify_title(shopify_title))
    farmina_tokens = _tokenize(entry["slug"] + " " + entry["name"])
    if not farmina_tokens:
        return 0.0
    inter = shopify_tokens & farmina_tokens
    union = shopify_tokens | farmina_tokens
    return len(inter) / len(union) if union else 0.0


def find_best_match(shopify_title: str, catalog: list) -> dict | None:
    title_lower = shopify_title.lower()
    if any(w in title_lower for w in ["canine", "perro", "dog"]):
        candidates = [e for e in catalog if e["species"] == "dog"]
    elif any(w in title_lower for w in ["feline", "gato", "cat"]):
        candidates = [e for e in catalog if e["species"] == "cat"]
    else:
        candidates = catalog

    if not candidates:
        return None

    scored = sorted(
        [(e, _score(shopify_title, e)) for e in candidates],
        key=lambda x: x[1], reverse=True,
    )
    best_entry, best_score = scored[0]

    log.info(f"  Top 3 matches para '{shopify_title}':")
    for e, s in scored[:3]:
        log.info(f"    [{s:.2f}] {e['name']} ({e['slug']})")

    if best_score < 0.10:
        log.warning(f"  Score demasiado bajo ({best_score:.2f}) — sin match")
        return None

    return best_entry

# ─── Procesamiento de imágenes ────────────────────────────────────────────────

def _has_transparency(img: Image.Image) -> bool:
    return img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info)


def process_image(img: Image.Image) -> Image.Image:
    """2000×2000, centrada, fondo blanco."""
    transparent = _has_transparency(img)
    img_conv = img.convert("RGBA") if transparent else img.convert("RGB")

    ratio = img_conv.width / img_conv.height
    if ratio > 1:
        new_w, new_h = TARGET_SIZE[0], int(TARGET_SIZE[0] / ratio)
    else:
        new_w, new_h = int(TARGET_SIZE[1] * ratio), TARGET_SIZE[1]

    resized    = img_conv.resize((new_w, new_h), Image.LANCZOS)
    background = Image.new("RGB", TARGET_SIZE, (255, 255, 255))
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
    parser.add_argument("--rebuild-catalog", action="store_true",
                        help="Forzar reconstrucción del catálogo farmina.com")
    args = parser.parse_args()

    if not CLIENT_ID or not CLIENT_SECRET:
        log.error("Faltan CLIENT_ID o CLIENT_SECRET")
        sys.exit(1)

    OUTPUT_DIR.mkdir(exist_ok=True)
    Path("resultados").mkdir(exist_ok=True)

    log.info("=" * 60)
    log.info("PROCESADOR FARMINA VET LIFE — SHOPIFY")
    log.info("=" * 60)

    if args.rebuild_catalog and CATALOG_FILE.exists():
        CATALOG_FILE.unlink()
        log.info("Catálogo eliminado, reconstruyendo...")

    catalog = build_farmina_catalog()
    if not catalog:
        log.error("Catálogo vacío — abortando")
        sys.exit(1)

    token = get_token()
    api   = ShopifyAPI(token)

    if args.product_id:
        log.info(f"Modo prueba — producto ID: {args.product_id}")
        products = [api.get_product(args.product_id)]
    else:
        log.info(f"Obteniendo productos '{VENDOR}'...")
        products = api.get_products(VENDOR)
        log.info(f"Total: {len(products)} productos\n")

    stats = dict(total=len(products), actualizadas=0, sin_match=0, errores=0)

    for i, product in enumerate(products, 1):
        pid   = product["id"]
        title = product["title"]
        log.info(f"\n[{i}/{len(products)}] {title}  (ID: {pid})")

        try:
            match = find_best_match(title, catalog)
            if not match:
                log.warning(f"  Sin match en farmina.com — saltando")
                stats["sin_match"] += 1
                continue

            log.info(f"  ✓ Match: '{match['name']}' → {match['image_url'].split('/')[-1]}")

            official_img = download_image(match["image_url"])
            processed    = process_image(official_img)
            b64          = to_b64_jpeg(processed)
            fname        = f"vetlife_{pid}_oficial.jpg"
            processed.save(OUTPUT_DIR / fname, "JPEG",
                           quality=JPEG_QUALITY, optimize=True)
            log.info(f"  Procesada: {processed.size} → {fname}")

            images = api.get_images(pid)
            for j, img_data in enumerate(images):
                api.delete_image(pid, img_data["id"])
                time.sleep(0.2)
                log.info(f"  Imagen antigua {j+1}/{len(images)} borrada")

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
    log.info(f"  Productos procesados  : {stats['total']}")
    log.info(f"  Imágenes actualizadas : {stats['actualizadas']}")
    log.info(f"  Sin match farmina.com : {stats['sin_match']}")
    log.info(f"  Errores               : {stats['errores']}")


if __name__ == "__main__":
    main()
