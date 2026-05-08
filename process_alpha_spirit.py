#!/usr/bin/env python3
"""
Procesador de imágenes Alpha Spirit para Shopify
=================================================
1. Carga el catálogo desde caché (resultados/alpha_spirit_catalog.json)
   o lo reconstruye con Playwright si se pasa --rebuild-catalog
2. Para cada producto Shopify, busca el mejor match por nombre
3. Descarga, procesa (2000×2000 JPG, fondo blanco) y sube a Shopify
4. Elimina imágenes antiguas y sube la nueva como posición 1
"""

import os
import re
import sys
import time
import json
import base64
import logging
import argparse
import unicodedata
import requests
from pathlib import Path
from io import BytesIO
from PIL import Image
from dotenv import load_dotenv

load_dotenv()

SHOP_DOMAIN   = os.getenv("SHOP_DOMAIN",   "7ev1zx-eg.myshopify.com")
CLIENT_ID     = os.getenv("CLIENT_ID",     "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
VENDOR        = "Alpha Spirit"
API_VERSION   = "2024-10"
TARGET_SIZE   = (2000, 2000)
JPEG_QUALITY  = 85
OUTPUT_DIR    = Path("imagenes_alpha_spirit")
CATALOG_FILE  = Path("resultados/alpha_spirit_catalog.json")

STORE_BASE      = "https://www.aspiritpetfood.store"
COLLECTION_SLUG = "all"
VENDOR_FILTERS  = {"alpha spirit", "primal", "real food", "iberian", "alpha spirit store"}

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
        logging.FileHandler("alpha_spirit_images.log", encoding="utf-8"),
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

# ─── Catálogo aspiritpetfood.store ───────────────────────────────────────────

def build_catalog() -> list:
    if CATALOG_FILE.exists():
        catalog = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
        log.info(f"Catálogo cargado desde caché: {len(catalog)} productos")
        return catalog

    log.info("Construyendo catálogo con Playwright...")
    from playwright.sync_api import sync_playwright

    catalog = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="es-ES",
            extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]},
        )
        page = ctx.new_page()

        page_num = 1
        while True:
            url = (f"{STORE_BASE}/collections/{COLLECTION_SLUG}"
                   f"/products.json?limit=250&page={page_num}")
            log.info(f"  Cargando página {page_num}: {url}")
            page.goto(url, timeout=40000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            raw = page.evaluate("() => document.body.innerText")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                log.warning(f"  No se pudo parsear JSON en página {page_num} — parando")
                break

            products = data.get("products", [])
            if not products:
                log.info(f"  Página {page_num} vacía — fin del catálogo")
                break

            log.info(f"  Página {page_num}: {len(products)} productos")
            for p in products:
                vendor = p.get("vendor", "")
                if vendor.lower() not in VENDOR_FILTERS:
                    continue
                images = p.get("images", [])
                catalog.append({
                    "id":           p["id"],
                    "title":        p["title"],
                    "handle":       p["handle"],
                    "vendor":       vendor,
                    "product_type": p.get("product_type", ""),
                    "image_url":    images[0]["src"] if images else "",
                    "tags":         p.get("tags", []),
                })

            page_num += 1
            time.sleep(1)

        browser.close()

    seen = set()
    deduped = []
    for entry in catalog:
        if entry["handle"] not in seen:
            seen.add(entry["handle"])
            deduped.append(entry)

    CATALOG_FILE.parent.mkdir(exist_ok=True)
    CATALOG_FILE.write_text(
        json.dumps(deduped, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(f"Catálogo guardado: {len(deduped)} productos → {CATALOG_FILE}")
    return deduped

# ─── Algoritmo de matching ────────────────────────────────────────────────────

def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )

_ES_TO_EN = {
    "pollo":         "chicken",
    "pescado":       "fish",
    "cordero":       "lamb",
    "pato":          "duck",
    "ternera":       "beef",
    "conejo":        "rabbit",
    "salmon":        "salmon",
    "trucha":        "trout",
    "atun":          "fish",
    "dorada":        "seabream",
    "jamon":         "ham",
    "pavo":          "turkey",
    "cerdo":         "pork",
    "vaca":          "beef",
    "buey":          "beef",
    "ciervo":        "venison",
    "venado":        "venison",
    "jabali":        "boar",
    "semihúmedo":    "semiwet",
    "semihumedo":    "semiwet",
    "humedo":        "wet",
    "caja":          "wet",
    "tarro":         "wet",
    "seco":          "dry",
    "cachorro":      "puppy",
    "cachorros":     "puppies",
    "adulto":        "adult",
    "adultos":       "adult",
    "snack":         "snack",
    "snacks":        "snack",
    "multiproteico": "multiprotein",
    "sardina":       "sardine",
    "boqueron":      "anchovy",
    "iberico":       "iberian",
    "toro":          "beef",
    "puppy":         "puppies",
}

_STOPWORDS = {
    "the", "and", "for", "con", "para", "del", "los", "las", "una", "que",
    "mas", "food", "alimento", "diet", "de", "la", "el", "en", "y",
}
_IGNORE_TOKENS = {
    "alpha", "spirit", "alphaspirit", "aspiritpetfood",
    "500g", "2kg", "9kg", "14kg", "35g", "85g", "300g", "150g",
    "gr", "kg", "g", "x", "pack",
}


def _preprocess(text: str) -> str:
    t = _strip_accents(text.lower())
    t = re.sub(r'semi[\s\-]?h[uú]medo', 'semiwet', t)
    t = re.sub(r'\d+[x×]\d+\w*', '', t)
    t = re.sub(r'\d+[\.,]?\d*\s*(kg|g|gr|l|ml|tab|und)\b', '', t,
               flags=re.IGNORECASE)
    t = re.sub(r'\(.*?\)', '', t)
    words = re.split(r'(\s+)', t)
    t = "".join(_ES_TO_EN.get(w.strip(), w) for w in words)
    return t


def _tokenize(text: str) -> set:
    t = _preprocess(text)
    tokens = set(re.split(r'[\s\-_&+•\./,]+', t))
    return tokens - _STOPWORDS - _IGNORE_TOKENS - {''}


def _is_wet(title: str) -> bool:
    t = title.upper()
    return any(x in t for x in ["CAJA", "TARRO", "HUMEDO", "HÚMEDO",
                                 "WET", "MOUSSE", "PATE", "PATÉ",
                                 "ESTOFADO", "SALCHICHA", "LATA",
                                 "ALBONDIG"])


def _is_semiwet(title: str) -> bool:
    t = title.upper()
    return any(x in t for x in ["SEMI", "SEMI-HUMEDO", "SEMI-HÚMEDO", "SEMIWET"])


def _is_snack(title: str) -> bool:
    t = title.lower()
    return any(x in t for x in ["snack", "barrita", "bocadito", "hueso",
                                 "premio", "treat", "lonchita", "ristra",
                                 "nervio", "oreja"])


def _score(shopify_title: str, entry: dict) -> float:
    shopify_tokens = _tokenize(shopify_title)
    catalog_text   = entry["title"] + " " + entry["handle"] + " " + entry.get("product_type", "")
    catalog_tokens = _tokenize(catalog_text)
    if not catalog_tokens:
        return 0.0
    inter = shopify_tokens & catalog_tokens
    union = shopify_tokens | catalog_tokens
    return len(inter) / len(union) if union else 0.0


_DOG_KW = {"perros", "perro", "canine", "canino", "dog"}
_CAT_KW = {"gatos", "gato", "feline", "felino", "kitten", "cat"}


def _catalog_has_species(entry: dict, keywords: set) -> bool:
    t = entry["title"].lower()
    h = entry["handle"].lower()
    return any(kw in t or kw in h for kw in keywords)


def find_best_match(shopify_title: str, catalog: list) -> tuple[dict | None, float]:
    title_lower = shopify_title.lower()

    if any(w in title_lower for w in ["canine", "canino", "perro"]):
        candidates = [e for e in catalog if _catalog_has_species(e, _DOG_KW)
                      or not _catalog_has_species(e, _CAT_KW)]
    elif any(w in title_lower for w in ["feline", "felino", "gato"]):
        candidates = [e for e in catalog if _catalog_has_species(e, _CAT_KW)
                      or not _catalog_has_species(e, _DOG_KW)]
    else:
        candidates = catalog

    if not candidates:
        candidates = catalog

    is_snack   = _is_snack(shopify_title)
    is_wet     = _is_wet(shopify_title)
    is_semiwet = _is_semiwet(shopify_title)

    if is_snack:
        snack_candidates = [e for e in candidates
                            if _is_snack(e["title"]) or _is_snack(e["handle"])]
        if snack_candidates:
            candidates = snack_candidates
    elif is_semiwet:
        semiwet_candidates = [e for e in candidates
                              if _is_semiwet(e["title"]) or _is_semiwet(e["handle"])
                              or "semi" in e["handle"].lower()]
        if semiwet_candidates:
            candidates = semiwet_candidates
    elif is_wet:
        wet_candidates = [e for e in candidates
                          if _is_wet(e["title"]) or _is_wet(e["handle"])
                          or "wet" in e["handle"].lower() or "humedo" in e["handle"].lower()]
        if wet_candidates:
            candidates = wet_candidates

    scored = sorted(
        [(e, _score(shopify_title, e)) for e in candidates],
        key=lambda x: x[1], reverse=True,
    )

    if not scored:
        return None, 0.0

    best_entry, best_score = scored[0]

    bucket = "snack" if is_snack else ("semiwet" if is_semiwet else ("wet" if is_wet else "dry"))
    log.info(f"  [{bucket}] Top 3 matches:")
    for e, s in scored[:3]:
        log.info(f"    [{s:.2f}] {e['title']} ({e['handle']})")

    return (best_entry, best_score) if best_score >= 0.10 else (None, best_score)

# ─── Procesamiento de imágenes ────────────────────────────────────────────────

def _has_transparency(img: Image.Image) -> bool:
    return img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info)


PADDING = 0.05


def process_image(img: Image.Image) -> Image.Image:
    """2000×2000, centrada con margen, fondo blanco."""
    transparent = _has_transparency(img)
    img_conv = img.convert("RGBA") if transparent else img.convert("RGB")

    max_w = int(TARGET_SIZE[0] * (1 - 2 * PADDING))
    max_h = int(TARGET_SIZE[1] * (1 - 2 * PADDING))

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
    parser.add_argument("--only-ids", type=str, default=None,
                        help="Lista de IDs separados por coma a re-procesar (ej: 123,456)")
    parser.add_argument("--rebuild-catalog", action="store_true",
                        help="Forzar reconstrucción del catálogo aspiritpetfood.store")
    args = parser.parse_args()

    if not CLIENT_ID or not CLIENT_SECRET:
        log.error("Faltan CLIENT_ID o CLIENT_SECRET")
        sys.exit(1)

    OUTPUT_DIR.mkdir(exist_ok=True)
    Path("resultados").mkdir(exist_ok=True)

    log.info("=" * 60)
    log.info("PROCESADOR ALPHA SPIRIT — SHOPIFY")
    log.info("=" * 60)

    if args.rebuild_catalog and CATALOG_FILE.exists():
        CATALOG_FILE.unlink()
        log.info("Catálogo eliminado, reconstruyendo...")

    catalog = build_catalog()
    if not catalog:
        log.error("Catálogo vacío — abortando")
        sys.exit(1)

    token = get_token()
    api   = ShopifyAPI(token)

    only_ids_raw = args.only_ids or os.getenv("PRODUCT_IDS", "")
    only_ids: set[int] = set()
    if only_ids_raw:
        only_ids = {int(x.strip()) for x in only_ids_raw.split(",") if x.strip()}
        log.info(f"Modo re-proceso — filtrando {len(only_ids)} IDs específicos")

    if args.product_id:
        log.info(f"Modo prueba — producto ID: {args.product_id}")
        products = [api.get_product(args.product_id)]
    elif only_ids:
        log.info(f"Obteniendo {len(only_ids)} productos específicos...")
        products = [api.get_product(pid) for pid in only_ids]
        log.info(f"Total a procesar: {len(products)} productos\n")
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
            match, score = find_best_match(title, catalog)
            if not match:
                log.warning(f"  Sin match en catálogo (score={score:.2f}) — saltando")
                stats["sin_match"] += 1
                continue

            log.info(f"  ✓ Match: '{match['title']}' (score={score:.2f})")

            if not match["image_url"]:
                log.warning(f"  Match sin imagen — saltando")
                stats["sin_match"] += 1
                continue

            official_img = download_image(match["image_url"])
            processed    = process_image(official_img)
            b64          = to_b64_jpeg(processed)
            fname        = f"alpha_spirit_{pid}_oficial.jpg"
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
    log.info(f"  Sin match             : {stats['sin_match']}")
    log.info(f"  Errores               : {stats['errores']}")


if __name__ == "__main__":
    main()
