#!/usr/bin/env python3
"""
Orquestador principal — procesamiento de imágenes por marca.

Modos de uso:
  --backup                     Descarga imágenes de Shopify → backups/<slug>/
  --fuente shopify_backup      Lee backups/<slug>/, procesa, sube a Shopify
  --fuente web_oficial         Scrapea web fabricante (requiere marcas/<slug>.py)
  --fuente web_y_amazon        Web + búsqueda DDG adicional

Argumentos:
  --vendor  VENDOR             Vendor exacto en Shopify
  --web-url URL                URL web fabricante
  --product-id ID              Procesar solo este producto
  --only-ids ID1,ID2,...       Procesar solo estos IDs (también PRODUCT_IDS env)
  --rebuild-catalog            Forzar re-scraping del catálogo web
"""

import argparse
import importlib
import json
import logging
import os
import re
import sys
import time
from io import BytesIO
from pathlib import Path

import requests
from dotenv import load_dotenv
from PIL import Image

# Añadir raíz del repo al path para que los imports funcionen
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.image_utils import is_high_res, process_image, process_image_webp_only, to_webp_b64
from core.shopify_api import ShopifyAPI, get_token

load_dotenv()

BACKUPS_DIR  = Path("backups")
RESULTS_DIR  = Path("resultados")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "es-ES,es;q=0.9",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def vendor_slug(vendor: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", vendor.lower()).strip("_")


def download_raw(url: str) -> tuple:
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
    if ext not in ("jpg", "jpeg", "png", "webp"):
        ext = "jpg"
    elif ext == "jpeg":
        ext = "jpg"
    return r.content, ext


def search_ddg_images(query: str, exclude_domain: str = "",
                      max_results: int = 5) -> list:
    """Busca imágenes de alta resolución en DuckDuckGo."""
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        log.warning("  [DDG] duckduckgo-search no instalado")
        return []

    log.info(f"  [DDG] «{query}»")
    found = []
    try:
        with DDGS() as ddgs:
            hits = list(ddgs.images(query, max_results=max_results * 4,
                                    size="Large"))
        for r in hits:
            img_url = r.get("image", "")
            if not img_url or (exclude_domain and exclude_domain in img_url):
                continue
            try:
                raw, ext = download_raw(img_url)
                ok, w, h = is_high_res(raw)
                domain = img_url.split("/")[2] if img_url.startswith("http") else "?"
                if ok:
                    log.info(f"  [DDG] ✓ {domain}  {w}×{h}")
                    found.append((raw, ext))
                    if len(found) >= max_results:
                        break
                else:
                    log.debug(f"  [DDG] baja res {w}×{h} ({domain})")
            except Exception as e:
                log.debug(f"  [DDG] {e}")
            time.sleep(0.5)
    except Exception as e:
        log.warning(f"  [DDG] Error: {e}")
    return found


def _replace_images(api: ShopifyAPI, pid: int, title: str,
                    slug: str, processed: list):
    """Elimina las imágenes existentes y sube las procesadas."""
    existing = api.get_images(pid)

    # Capturar metadata original (alt + nombre de archivo) antes de borrar
    orig_meta = {}
    for img_data in existing:
        pos = img_data.get("position", 0)
        src = img_data.get("src", "").split("?")[0]
        stem = src.split("/")[-1].rsplit(".", 1)[0] if src else ""
        orig_meta[pos] = {
            "alt":      img_data.get("alt") or title,
            "filename": f"{stem}.webp" if stem else f"{slug}_{pid}_{pos}.webp",
        }

    for img_data in existing:
        api.delete_image(pid, img_data["id"])
        time.sleep(0.2)
    log.info(f"  {len(existing)} imagen(es) antigua(s) eliminada(s)")

    for pos, img in enumerate(processed, 1):
        meta  = orig_meta.get(pos, {})
        fname = meta.get("filename") or f"{slug}_{pid}_{pos}.webp"
        alt   = meta.get("alt") or title
        api.upload_image(pid, to_webp_b64(img), fname, alt=alt, position=pos)
        log.info(f"  ✓ {pos}/{len(processed)} subida  [{fname}]")
        time.sleep(0.5)


def _print_stats(stats: dict):
    log.info("\n" + "=" * 60)
    log.info("RESUMEN")
    log.info("=" * 60)
    for k, v in stats.items():
        log.info(f"  {k:<14}: {v}")


# ─── Modo backup ──────────────────────────────────────────────────────────────

def run_backup(api: ShopifyAPI, vendor: str):
    """Descarga todas las imágenes del vendor de Shopify a backups/<slug>/."""
    slug = vendor_slug(vendor)
    backup_root = BACKUPS_DIR / slug
    backup_root.mkdir(parents=True, exist_ok=True)

    log.info(f"Cargando productos de '{vendor}'...")
    products = api.get_products(vendor)
    log.info(f"Total: {len(products)} productos")

    backed_up = skipped = 0
    for product in products:
        pid    = product["id"]
        title  = product["title"]
        images = product.get("images", [])

        if not images:
            log.info(f"  [{pid}] {title[:50]} — sin imágenes")
            skipped += 1
            continue

        folder = backup_root / str(pid)
        folder.mkdir(exist_ok=True)

        metadata = {}
        for j, img_data in enumerate(images, 1):
            src = img_data.get("src", "")
            if not src:
                continue
            src = src.split("?")[0]
            try:
                raw, ext = download_raw(src)
                key  = f"img_{j:02d}"
                path = folder / f"{key}.{ext}"
                path.write_bytes(raw)
                stem = src.split("/")[-1].rsplit(".", 1)[0]
                metadata[key] = {
                    "alt":      img_data.get("alt") or "",
                    "filename": f"{stem}.webp" if stem else f"{slug}_{pid}_{j}.webp",
                    "position": img_data.get("position", j),
                }
                log.info(f"  [{pid}] {key}.{ext}")
            except Exception as e:
                log.warning(f"  [{pid}] Error img {j}: {e}")
            time.sleep(0.15)

        (folder / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2)
        )
        backed_up += 1

    log.info(f"\nBackup: {backed_up} con imágenes, {skipped} sin imágenes")


# ─── Modo shopify_backup ───────────────────────────────────────────────────────

def run_shopify_backup(api: ShopifyAPI, vendor: str,
                       product_id: int = None, only_ids: set = None,
                       pipeline: str = "standard"):
    """Lee imágenes de backups/<slug>/, las procesa y las sube a Shopify."""
    slug = vendor_slug(vendor)
    backup_root = BACKUPS_DIR / slug

    if not backup_root.exists():
        log.error(f"No existe backup para '{vendor}'. "
                  f"Ejecuta primero 'Backup imágenes de marca'.")
        sys.exit(1)

    if product_id:
        products = [api.get_product(product_id)]
    elif only_ids:
        products = [api.get_product(pid) for pid in only_ids]
    else:
        products = api.get_products(vendor)

    stats = dict(total=len(products), ok=0, sin_backup=0, errores=0)

    for i, product in enumerate(products, 1):
        pid   = product["id"]
        title = product["title"]
        log.info(f"\n[{i}/{len(products)}] {title}  (ID: {pid})")

        folder = backup_root / str(pid)
        if not folder.exists() or not any(folder.iterdir()):
            log.warning("  Sin backup — saltando")
            stats["sin_backup"] += 1
            continue

        processed = []
        for img_path in sorted(folder.iterdir()):
            if img_path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                continue
            try:
                raw = img_path.read_bytes()
                img = Image.open(BytesIO(raw))
                log.info(f"  {img_path.name}: {img.mode} {img.size}")
                fn = process_image_webp_only if pipeline == "webp_only" else process_image
                processed.append(fn(img))
            except Exception as e:
                log.warning(f"  Error procesando {img_path.name}: {e}")

        if not processed:
            log.warning("  Ninguna imagen procesada — saltando")
            stats["errores"] += 1
            continue

        _replace_images(api, pid, title, slug, processed)
        stats["ok"] += 1
        time.sleep(1)

    _print_stats(stats)


# ─── Modo web_oficial / web_y_amazon ──────────────────────────────────────────

def run_web(api: ShopifyAPI, vendor: str, web_url: str, fuente: str,
            rebuild: bool, product_id: int = None, only_ids: set = None):
    """Scrapea el catálogo web del fabricante, procesa y sube a Shopify."""
    slug = vendor_slug(vendor)

    try:
        scraper = importlib.import_module(f"marcas.{slug}")
    except ImportError:
        log.error(f"No existe scraper para '{vendor}'. "
                  f"Crea marcas/{slug}.py con scrape_catalog() y find_best_match().")
        sys.exit(1)

    RESULTS_DIR.mkdir(exist_ok=True)
    catalog = scraper.scrape_catalog(web_url, rebuild=rebuild)
    if not catalog:
        log.error("Catálogo vacío — revisa el scraping")
        sys.exit(1)

    if product_id:
        products = [api.get_product(product_id)]
    elif only_ids:
        products = [api.get_product(pid) for pid in only_ids]
    else:
        products = api.get_products(vendor)

    stats = dict(total=len(products), ok=0, sin_match=0, sin_imagen=0, errores=0)
    try:
        exclude_domain = web_url.split("/")[2]
    except Exception:
        exclude_domain = ""

    for i, product in enumerate(products, 1):
        pid   = product["id"]
        title = product["title"]
        log.info(f"\n[{i}/{len(products)}] {title}  (ID: {pid})")

        handle, score = scraper.find_best_match(title, catalog)
        if handle is None or score < 0.10:
            log.warning(f"  Sin match (score={score:.2f}) — saltando")
            stats["sin_match"] += 1
            continue

        entry = catalog[handle]
        log.info(f"  Match: {handle}  (score={score:.2f}, "
                 f"{len(entry.get('images', []))} imgs)")

        raw_images = []
        for img_url in entry.get("images", []):
            try:
                raw, ext = download_raw(img_url)
                ok, w, h = is_high_res(raw)
                fname = img_url.split("/")[-1].split("?")[0]
                if ok:
                    raw_images.append((raw, ext))
                    log.info(f"  Descargada: {fname}  {w}×{h}")
                else:
                    log.warning(f"  Baja res {w}×{h} — omitida: {fname}")
            except Exception as e:
                log.warning(f"  Error descargando: {e}")

        if not raw_images and fuente == "web_y_amazon":
            log.warning("  Sin imágenes web oficial — buscando en internet...")
            raw_images = search_ddg_images(
                f"{vendor} {title} product",
                exclude_domain=exclude_domain,
            )

        if not raw_images:
            log.warning("  Sin imágenes de alta resolución — saltando")
            stats["sin_imagen"] += 1
            continue

        processed = []
        for j, (raw, _) in enumerate(raw_images, 1):
            try:
                img = Image.open(BytesIO(raw))
                log.info(f"  Imagen {j}: {img.mode} {img.size}")
                processed.append(process_image(img))
            except Exception as e:
                log.warning(f"  Error procesando imagen {j}: {e}")

        if not processed:
            stats["errores"] += 1
            continue

        _replace_images(api, pid, title, slug, processed)
        stats["ok"] += 1
        time.sleep(1)

    _print_stats(stats)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor",          required=True)
    parser.add_argument("--fuente",
                        choices=["shopify_backup", "web_oficial", "web_y_amazon"])
    parser.add_argument("--web-url",         default="")
    parser.add_argument("--product-id",      type=int, default=None)
    parser.add_argument("--only-ids",        default="")
    parser.add_argument("--rebuild-catalog", action="store_true")
    parser.add_argument("--backup",          action="store_true")
    parser.add_argument("--pipeline",
                        choices=["standard", "webp_only"],
                        default="standard")
    args = parser.parse_args()

    if not os.getenv("CLIENT_ID") or not os.getenv("CLIENT_SECRET"):
        log.error("Faltan CLIENT_ID / CLIENT_SECRET")
        sys.exit(1)

    log.info("=" * 60)
    mode = "BACKUP" if args.backup else (args.fuente or "?").upper()
    log.info(f"  Vendor   : {args.vendor}")
    log.info(f"  Modo     : {mode}")
    log.info(f"  Pipeline : {args.pipeline}")
    if args.web_url:
        log.info(f"  Web    : {args.web_url}")
    log.info("=" * 60)

    token = get_token()
    log.info("Token obtenido")
    api = ShopifyAPI(token)

    only_ids: set = set()
    for raw in [args.only_ids, os.getenv("PRODUCT_IDS", "")]:
        if raw:
            only_ids |= {int(x.strip()) for x in raw.split(",") if x.strip()}

    if args.backup:
        run_backup(api, args.vendor)
    elif args.fuente == "shopify_backup":
        run_shopify_backup(api, args.vendor, args.product_id, only_ids or None,
                           pipeline=args.pipeline)
    elif args.fuente in ("web_oficial", "web_y_amazon"):
        if not args.web_url:
            log.error("--web-url requerido para fuente web_oficial / web_y_amazon")
            sys.exit(1)
        run_web(api, args.vendor, args.web_url, args.fuente,
                args.rebuild_catalog, args.product_id, only_ids or None)
    else:
        log.error("Especifica --backup o --fuente")
        sys.exit(1)


if __name__ == "__main__":
    main()
