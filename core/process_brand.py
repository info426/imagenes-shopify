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
import inspect
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import unicodedata

import requests
from dotenv import load_dotenv
from PIL import Image

# Añadir raíz del repo al path para que los imports funcionen
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.image_utils import (dedupe_images, is_high_res, process_image,
                              process_image_webp_only, to_webp_b64)
from core.shopify_api import ShopifyAPI, get_token
from core import amazon

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


def title_slug(title: str) -> str:
    """Convierte el título del producto en un slug SEO-friendly con guiones."""
    normalized = unicodedata.normalize("NFD", title.lower())
    ascii_str = normalized.encode("ascii", "ignore").decode()
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", ascii_str)).strip("-")


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
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            log.warning("  [DDG] Instala ddgs: pip install ddgs")
            return []

    log.info(f"  [DDG] «{query}»")
    hits = []
    for attempt in range(3):
        try:
            with DDGS() as ddgs:
                hits = list(ddgs.images(query, max_results=max_results * 4,
                                        size="Large"))
            break
        except Exception as e:
            wait = 3 * (2 ** attempt)  # 3s, 6s, 12s
            if attempt < 2:
                log.warning(f"  [DDG] Error (intento {attempt+1}/3): {e} — reintentando en {wait}s")
                time.sleep(wait)
            else:
                log.warning(f"  [DDG] Error tras 3 intentos: {e}")
                return []

    found = []
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
    return found



def _replace_images(api: ShopifyAPI, pid: int, title: str,
                    slug: str, processed: list):
    """Elimina las imágenes existentes y sube las procesadas."""
    existing = api.get_images(pid)

    # Capturar alt original antes de borrar; el nombre se genera desde el título
    t_slug = title_slug(title)
    orig_meta = {}
    for img_data in existing:
        pos = img_data.get("position", 0)
        orig_meta[pos] = {"alt": img_data.get("alt") or title}

    for img_data in existing:
        api.delete_image(pid, img_data["id"])
        time.sleep(0.2)
    log.info(f"  {len(existing)} imagen(es) antigua(s) eliminada(s)")

    for pos, img in enumerate(processed, 1):
        meta  = orig_meta.get(pos, {})
        fname = f"{t_slug}_{pos}.webp"
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


# ─── Metacampos de fuente (URL del fabricante) ──────────────────────────────────
#
# Guardamos la fuente oficial de cada producto en metacampos de Shopify para que
# los workflows (imágenes y, en el futuro, descripciones) no tengan que buscarla
# por DDG cada vez:
#   fuentes.url_fabricante   (url)  → URL activa que leen los workflows
#   fuentes.url_fabricante_2 (url)  → URL alternativa (otra versión/idioma/web)
#   fuentes.historico        (json) → registro append-only [{url, fecha, workflow, resultado}]

MF_NAMESPACE = "fuentes"
MF_KEY_URL   = "url_fabricante"
MF_KEY_URL_2 = "url_fabricante_2"
MF_KEYS_URL  = (MF_KEY_URL, MF_KEY_URL_2)
MF_KEY_HIST  = "historico"


def _read_source_urls(api: ShopifyAPI, pid: int) -> list:
    """Lee las URLs del fabricante (url_fabricante + url_fabricante_2).
    Devuelve la lista de URLs no vacías, sin duplicados, conservando el orden."""
    urls = []
    for key in MF_KEYS_URL:
        try:
            mf = api.get_metafield(pid, MF_NAMESPACE, key)
            val = (mf or {}).get("value") or ""
        except Exception as e:
            log.debug(f"  [metacampo] no se pudo leer {key}: {e}")
            val = ""
        if val and val not in urls:
            urls.append(val)
    return urls


def _save_source_url(api: ShopifyAPI, pid: int, url: str,
                     workflow: str, resultado: str,
                     url_key: str = MF_KEY_URL):
    """Guarda la URL en el metacampo indicado y la añade al histórico JSON (append-only)."""
    if not url:
        return
    try:
        api.set_metafield(pid, MF_NAMESPACE, url_key, url, "url")
        hist = []
        hist_mf = api.get_metafield(pid, MF_NAMESPACE, MF_KEY_HIST)
        if hist_mf and hist_mf.get("value"):
            try:
                hist = json.loads(hist_mf["value"])
                if not isinstance(hist, list):
                    hist = []
            except Exception:
                hist = []
        # No duplicar la última entrada si la URL no ha cambiado
        if not hist or hist[-1].get("url") != url:
            hist.append({
                "url":       url,
                "fecha":     datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "workflow":  workflow,
                "resultado": resultado,
            })
            api.set_metafield(pid, MF_NAMESPACE, MF_KEY_HIST,
                              json.dumps(hist, ensure_ascii=False), "json")
        log.info(f"  [metacampo] {url_key} guardada: {url}")
    except Exception as e:
        log.warning(f"  [metacampo] no se pudo guardar {url_key}: {e}")


def _clear_source_url(api: ShopifyAPI, pid: int, url_key: str):
    """Elimina el metacampo url_key si existe (p. ej. para limpiar URLs erróneas
    de un run anterior cuando el re-run no encuentra match para ese producto)."""
    try:
        deleted = api.delete_metafield(pid, MF_NAMESPACE, url_key)
        if deleted:
            log.info(f"  [metacampo] {url_key} eliminada (sin match en este run)")
        else:
            log.info(f"  [metacampo] {url_key} no existía — nada que limpiar")
    except Exception as e:
        log.warning(f"  [metacampo] no se pudo eliminar {url_key}: {e}")


# ─── Modo backup ──────────────────────────────────────────────────────────────

def run_backup(api: ShopifyAPI, vendor: str, force: bool = False):
    """Descarga todas las imágenes del vendor de Shopify a backups/<slug>/."""
    import shutil
    slug = vendor_slug(vendor)
    backup_root = BACKUPS_DIR / slug
    backup_root.mkdir(parents=True, exist_ok=True)

    log.info(f"Cargando productos de '{vendor}'...")
    products = api.get_products(vendor)
    log.info(f"Total: {len(products)} productos")

    backed_up = skipped = already = 0
    for product in products:
        pid    = product["id"]
        title  = product["title"]
        images = product.get("images", [])

        if not images:
            log.info(f"  [{pid}] {title[:50]} — sin imágenes")
            skipped += 1
            continue

        folder = backup_root / str(pid)
        if folder.exists() and any(f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
                                   for f in folder.iterdir()):
            if not force:
                log.info(f"  [{pid}] {title[:50]} — backup existente, saltando")
                already += 1
                continue
            shutil.rmtree(folder)
            log.info(f"  [{pid}] {title[:50]} — backup anterior eliminado (force)")

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
                stem = re.sub(r"_[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", "", stem)
                fname = f"{stem}_{pid}_{j}.webp" if stem else f"{slug}_{pid}_{j}.webp"
                metadata[key] = {
                    "alt":      img_data.get("alt") or "",
                    "filename": fname,
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

    log.info(f"\nBackup: {backed_up} nuevos, {already} ya existían (no sobreescritos), {skipped} sin imágenes")


# ─── Modo shopify_backup ───────────────────────────────────────────────────────

def run_shopify_backup(api: ShopifyAPI, vendor: str,
                       product_id: int = None, only_ids: set = None,
                       pipeline: str = "standard", force_padding: bool | None = None):
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

    # Registro de productos ya procesados (persiste entre ejecuciones via git)
    RESULTS_DIR.mkdir(exist_ok=True)
    log_path = RESULTS_DIR / f"{slug}_processed.json"
    done_ids: set = set()
    if log_path.exists():
        try:
            done_ids = set(json.loads(log_path.read_text()))
        except Exception:
            pass

    stats = dict(total=len(products), ok=0, saltados=0, sin_backup=0, errores=0)

    for i, product in enumerate(products, 1):
        pid   = product["id"]
        title = product["title"]
        log.info(f"\n[{i}/{len(products)}] {title}  (ID: {pid})")

        if pid in done_ids:
            log.info("  Ya procesado — saltando")
            stats["saltados"] += 1
            continue

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
                kwargs = {} if pipeline == "webp_only" else {"force_padding": force_padding}
                processed.append(fn(img, **kwargs))
            except Exception as e:
                log.warning(f"  Error procesando {img_path.name}: {e}")

        if not processed:
            log.warning("  Ninguna imagen procesada — saltando")
            stats["errores"] += 1
            continue

        _replace_images(api, pid, title, slug, processed)
        done_ids.add(pid)
        log_path.write_text(json.dumps(sorted(done_ids)))
        stats["ok"] += 1
        time.sleep(1)

    _print_stats(stats)


# ─── Modo web_oficial / web_y_amazon ──────────────────────────────────────────

# Marcadores internos que degradan las búsquedas DDG
_TITLE_NOISE = re.compile(r'\s*\((?:NDR|PV|NV|ONLINE)\)\s*', re.IGNORECASE)


def _clean_title_for_ddg(title: str) -> str:
    """Limpia el título Shopify para usarlo como query DDG (elimina marcadores internos)."""
    return _TITLE_NOISE.sub(" ", title).strip() + " product image"


def _download_hires(urls: list, label: str) -> list:
    """Descarga URLs y conserva solo las que cumplen la resolución mínima (800px).
    Devuelve lista [(raw_bytes, ext), ...]."""
    out = []
    for url in urls:
        try:
            raw, ext = download_raw(url)
            ok, w, h = is_high_res(raw)
            fname = url.split("/")[-1].split("?")[0]
            if ok:
                out.append((raw, ext))
                log.info(f"  [{label}] ✓ {fname}  {w}×{h}")
            else:
                log.warning(f"  [{label}] baja res {w}×{h} — omitida: {fname}")
        except Exception as e:
            log.warning(f"  [{label}] error descargando: {e}")
    return out


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
        log.warning("Catálogo vacío — el scraper deberá resolver cada producto bajo demanda")

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

        # Extraer primer barcode no vacío (EAN) para scrapers que lo admitan
        barcode = next(
            (str(v.get("barcode", "")).strip()
             for v in product.get("variants", [])
             if v.get("barcode")),
            ""
        )

        handle, score = None, 0.0
        web_matched = False
        raw_images = []

        # Fuente 1a — Override por metacampo: si el producto tiene
        # fuentes.url_fabricante (y/o url_fabricante_2), scrapeamos esas URLs
        # directamente y nos saltamos DDG/matching (más rápido y sin falsos
        # positivos). Con dos URLs (p. ej. versión ES + EN del mismo producto)
        # se combinan las imágenes; dedupe_images conserva la de mayor resolución.
        source_urls = (_read_source_urls(api, pid)
                       if hasattr(scraper, "scrape_product_url") else [])
        if source_urls:
            log.info(f"  URLs fabricante (metacampo): {source_urls}")
            for su in source_urls:
                try:
                    entry = scraper.scrape_product_url(su, barcode=barcode)
                except Exception as e:
                    log.warning(f"  [metacampo] error scrapeando {su}: {e}")
                    entry = None
                if entry and entry.get("images"):
                    web_matched = True
                    log.info(f"  Match directo (metacampo): "
                             f"{len(entry['images'])} imgs — {su}")
                    raw_images += _download_hires(entry["images"], "web")
                else:
                    log.warning(f"  [metacampo] sin imágenes en {su}")
            if not web_matched:
                log.warning("  [metacampo] ninguna URL dio imágenes — fallback a matching")

        # Fuente 1b — matching estándar (DDG) si el override no resolvió
        if not web_matched:
            sig = inspect.signature(scraper.find_best_match)
            if "barcode" in sig.parameters:
                handle, score = scraper.find_best_match(title, catalog,
                                                         barcode=barcode)
            else:
                handle, score = scraper.find_best_match(title, catalog)

            web_matched = handle is not None and score >= 0.10
            if web_matched:
                entry = catalog[handle]
                log.info(f"  Match: {handle}  (score={score:.2f}, "
                         f"{len(entry.get('images', []))} imgs)")
                raw_images += _download_hires(entry.get("images", []), "web")
                # Auto-aprendizaje: persistir la URL resuelta para próximas veces
                resolved_url = entry.get("url")
                if resolved_url:
                    _save_source_url(api, pid, resolved_url, "imagenes",
                                     f"ddg score={score:.2f}")
            else:
                log.warning(f"  Sin match web (score={score:.2f})")

        # Fuente 2 — Amazon (solo en web_y_amazon; se combina con la web)
        if fuente == "web_y_amazon":
            # Usar caché del catálogo cuando exista para evitar re-scraping.
            # Las URLs de Amazon (CDN con hash en el nombre) son permanentes.
            cached_amazon = (catalog.get(handle, {}).get("amazon_images")
                             if web_matched else None)
            if cached_amazon:
                log.info(f"  [amazon] caché: {len(cached_amazon)} URLs")
                raw_images += _download_hires(cached_amazon, "amazon")
            else:
                try:
                    amazon_urls = amazon.search_amazon_image_urls(
                        title, barcode=barcode)
                    raw_images += _download_hires(amazon_urls, "amazon")
                    # Cachear SOLO si hay resultados reales. Una lista vacía suele
                    # ser un fallo transitorio (CAPTCHA / sin índice DDG); cachearla
                    # impediría reintentar en ejecuciones futuras.
                    if amazon_urls and web_matched and handle in catalog:
                        catalog[handle]["amazon_images"] = amazon_urls
                        if hasattr(scraper, "save_catalog"):
                            scraper.save_catalog(catalog)
                            log.info(f"  [amazon] {len(amazon_urls)} URLs "
                                     f"guardadas en catálogo")
                except Exception as e:
                    log.warning(f"  [amazon] fallo: {e}")

        # web_oficial sin match → no hay nada más que probar
        if not web_matched and fuente == "web_oficial":
            stats["sin_match"] += 1
            continue

        # Fuente 3 — último recurso: DDG genérico (solo web_y_amazon)
        if not raw_images and fuente == "web_y_amazon":
            log.warning("  Sin imágenes web/amazon — DDG genérico")
            ddg_query = (scraper.get_ddg_query(title)
                         if hasattr(scraper, "get_ddg_query")
                         else _clean_title_for_ddg(title))
            raw_images = search_ddg_images(ddg_query, exclude_domain=exclude_domain)

        if not raw_images:
            log.warning("  Sin imágenes de alta resolución — saltando")
            stats["sin_imagen"] += 1
            continue

        # Dedup perceptual entre fuentes: ante la misma imagen, conserva la
        # de mayor resolución (más bytes/área). Ver core.image_utils.dedupe_images
        raw_images = dedupe_images(raw_images)

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


# ─── Modo crear definiciones de metacampo ───────────────────────────────────────

def run_create_metafield_defs(api: ShopifyAPI):
    """Crea las definiciones de metacampo en Shopify para que aparezcan en el
    admin del producto y se puedan editar a mano (pegar las URLs)."""
    defs = [
        ("URL fabricante",       MF_KEY_URL,   "url",
         "URL oficial del producto en la web del fabricante (fuente activa)."),
        ("URL fabricante (2)",   MF_KEY_URL_2, "url",
         "URL alternativa del mismo producto (otra versión, idioma o web extendida)."),
        ("Histórico de fuentes", MF_KEY_HIST,  "json",
         "Registro de URLs de fuente usadas por los workflows (no editar a mano)."),
    ]
    mutation = """
    mutation CreateDef($definition: MetafieldDefinitionInput!) {
      metafieldDefinitionCreate(definition: $definition) {
        createdDefinition { id name namespace key }
        userErrors { field message code }
      }
    }
    """
    for name, key, mtype, desc in defs:
        variables = {"definition": {
            "name": name, "namespace": MF_NAMESPACE, "key": key,
            "type": mtype, "ownerType": "PRODUCT", "description": desc,
        }}
        try:
            res = api.graphql(mutation, variables).get("metafieldDefinitionCreate", {})
            errs = res.get("userErrors", [])
            if errs:
                if any(e.get("code") == "TAKEN" for e in errs):
                    log.info(f"  ✓ {MF_NAMESPACE}.{key} — ya existía")
                else:
                    log.warning(f"  ✗ {MF_NAMESPACE}.{key}: {errs}")
            else:
                log.info(f"  ✓ creada {MF_NAMESPACE}.{key} ({mtype})")
        except Exception as e:
            log.warning(f"  ✗ {MF_NAMESPACE}.{key}: {e}")


# ─── Modo backfill de URLs ──────────────────────────────────────────────────────

def run_backfill_urls(api: ShopifyAPI, vendor: str):
    """Importa las URLs ya resueltas en resultados/<slug>_catalog.json a los
    metacampos fuentes.url_fabricante de cada producto, sin lanzar DDG."""
    slug = vendor_slug(vendor)
    try:
        scraper = importlib.import_module(f"marcas.{slug}")
    except ImportError:
        log.error(f"No existe scraper para '{vendor}'.")
        sys.exit(1)

    key_fn = getattr(scraper, "title_cache_key", None)
    if key_fn is None:
        log.error(f"marcas/{slug}.py no expone title_cache_key(); no se puede "
                  f"mapear título Shopify → URL cacheada.")
        sys.exit(1)

    catalog = scraper.scrape_catalog("", rebuild=False)
    if not catalog:
        log.warning("Catálogo vacío — nada que importar.")
        return

    products = api.get_products(vendor)
    stats = dict(total=len(products), guardadas=0, sin_url=0)
    for i, product in enumerate(products, 1):
        pid, title = product["id"], product["title"]
        entry = catalog.get(key_fn(title))
        url = entry.get("url") if isinstance(entry, dict) else None
        if url:
            _save_source_url(api, pid, url, "backfill", "catalogo")
            stats["guardadas"] += 1
            log.info(f"[{i}/{len(products)}] {title[:50]} → {url}")
        else:
            stats["sin_url"] += 1
            log.info(f"[{i}/{len(products)}] {title[:50]} — sin URL cacheada")
        time.sleep(0.3)
    _print_stats(stats)


# ─── Modo resolver URLs (busca en la web y guarda el metacampo) ─────────────────

def _norm_url(u: str) -> str:
    """Normaliza una URL para comparar (sin esquema, sin www, sin barra final)."""
    if not u:
        return ""
    u = u.strip().split("#")[0].split("?")[0].rstrip("/")
    u = re.sub(r"^https?://", "", u, flags=re.IGNORECASE)
    u = re.sub(r"^www\.", "", u, flags=re.IGNORECASE)
    return u.lower()


def _compare_dryrun_vs_snapshot(slug: str, url_key: str, dry_results: list):
    """Compara lo que el resolver PONDRÍA (dry-run) contra el snapshot guardado
    (las URLs correctas verificadas a mano). Imprime un informe de precisión y
    lista las diferencias para revisar antes de un run real."""
    snap_path = RESULTS_DIR / f"{slug}_urls_snapshot.json"
    if not snap_path.exists():
        log.info(f"[dry-run] No hay snapshot ({snap_path.name}) — sin comparación.")
        return
    try:
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"[dry-run] no pude leer snapshot: {e}")
        return
    snap_by_id = {str(e.get("id")): (e.get(url_key) or "") for e in snap}
    dry_by_id  = {str(r["id"]): (r.get("url") or "") for r in dry_results}
    titles     = {str(r["id"]): r.get("title", "") for r in dry_results}

    cats = {"igual": [], "distinto": [], "resolver_borraria": [],
            "resolver_aporta": [], "ambos_vacio": []}
    for pid in dry_by_id:
        want = _norm_url(snap_by_id.get(pid, ""))
        got  = _norm_url(dry_by_id.get(pid, ""))
        if want and got:
            cats["igual" if want == got else "distinto"].append(pid)
        elif want and not got:
            cats["resolver_borraria"].append(pid)   # snapshot tiene, resolver no
        elif got and not want:
            cats["resolver_aporta"].append(pid)      # resolver tiene, snapshot no
        else:
            cats["ambos_vacio"].append(pid)

    base = [pid for pid in dry_by_id if _norm_url(snap_by_id.get(pid, ""))]
    igual = len(cats["igual"])
    log.info("\n" + "=" * 60)
    log.info(f"DRY-RUN vs SNAPSHOT ({url_key})")
    log.info("=" * 60)
    log.info(f"  Snapshot con URL          : {len(base)}")
    log.info(f"  ✓ Reproduce igual         : {igual}"
             + (f"  ({igual*100//len(base)}%)" if base else ""))
    log.info(f"  ✗ Distinto (REGRESIÓN)    : {len(cats['distinto'])}")
    log.info(f"  ✗ Borraría (sin_match)    : {len(cats['resolver_borraria'])}")
    log.info(f"  + Aporta nueva (snap vacío): {len(cats['resolver_aporta'])}")
    for pid in cats["distinto"]:
        log.info(f"    [DISTINTO] {titles.get(pid,'')[:42]}")
        log.info(f"       snapshot : {snap_by_id.get(pid,'')}")
        log.info(f"       resolver : {dry_by_id.get(pid,'')}")
    for pid in cats["resolver_borraria"]:
        log.info(f"    [BORRARÍA] {titles.get(pid,'')[:42]}  → snapshot tiene: "
                 f"{snap_by_id.get(pid,'')}")
    for pid in cats["resolver_aporta"]:
        log.info(f"    [APORTA]   {titles.get(pid,'')[:42]}  → {dry_by_id.get(pid,'')}")
    log.info("=" * 60)


def run_resolve_urls(api: ShopifyAPI, vendor: str, web_url: str,
                     rebuild: bool, product_id: int = None,
                     only_ids: set = None, url_key: str = MF_KEY_URL,
                     clear_on_no_match: bool = False, dry_run: bool = False):
    """Para cada producto resuelve su URL oficial vía el scraper de la marca
    (find_best_match: slug directo + DDG) y la guarda en el metacampo
    indicado por url_key (por defecto fuentes.url_fabricante). NO procesa imágenes.

    Si clear_on_no_match=True, elimina el metacampo url_key cuando no se
    encuentra URL (útil para limpiar valores erróneos de un run anterior).

    Si dry_run=True NO escribe en Shopify: guarda lo que pondría en
    resultados/{slug}_resolver_dryrun.json y, si existe el snapshot, lo compara."""
    slug = vendor_slug(vendor)
    try:
        scraper = importlib.import_module(f"marcas.{slug}")
    except ImportError:
        log.error(f"No existe scraper para '{vendor}'. "
                  f"Crea marcas/{slug}.py con scrape_catalog() y find_best_match().")
        sys.exit(1)

    RESULTS_DIR.mkdir(exist_ok=True)
    catalog = scraper.scrape_catalog(web_url, rebuild=rebuild)

    def _safe_get(pid):
        try:
            return api.get_product(pid)
        except Exception as e:
            log.warning(f"  No se pudo cargar el producto {pid} (¿es el ID de "
                        f"Shopify, no el EAN?): {e}")
            return None

    if product_id:
        products = [p for p in [_safe_get(product_id)] if p]
    elif only_ids:
        products = [p for p in (_safe_get(pid) for pid in only_ids) if p]
    else:
        products = api.get_products(vendor)

    fbm_params = inspect.signature(scraper.find_best_match).parameters
    has_barcode = "barcode" in fbm_params
    has_images  = "product_images" in fbm_params
    stats = dict(total=len(products), guardadas=0, sin_match=0)
    dry_results: list = []
    if dry_run:
        log.info("MODO DRY-RUN — no se escribirá nada en Shopify")
    for i, product in enumerate(products, 1):
        pid, title = product["id"], product["title"]
        log.info(f"\n[{i}/{len(products)}] {title}  (ID: {pid})")

        barcode = next(
            (str(v.get("barcode", "")).strip()
             for v in product.get("variants", [])
             if v.get("barcode")),
            ""
        )

        kwargs = {}
        if has_barcode:
            kwargs["barcode"] = barcode
        if has_images:
            # URLs de imagen del producto Shopify (CDN público) para el
            # reconocimiento visual tipo Google Lens dentro del scraper.
            kwargs["product_images"] = [
                im.get("src") for im in product.get("images", []) if im.get("src")
            ]
        handle, score = scraper.find_best_match(title, catalog, **kwargs)

        url = catalog.get(handle, {}).get("url") if handle else None
        matched = handle is not None and score >= 0.10 and url
        dry_results.append({"id": pid, "title": title,
                            "url": url if matched else None,
                            "score": round(score, 3)})
        if matched:
            stats["guardadas"] += 1
            if dry_run:
                log.info(f"  [dry-run] PONDRÍA → {url}  (score={score:.2f})")
            else:
                _save_source_url(api, pid, url, "resolver-urls",
                                 f"score={score:.2f}", url_key=url_key)
                log.info(f"  → {url}  (score={score:.2f})")
        else:
            stats["sin_match"] += 1
            log.warning(f"  Sin URL (score={score:.2f})")
            if clear_on_no_match and not dry_run:
                _clear_source_url(api, pid, url_key)
            elif clear_on_no_match and dry_run:
                log.info(f"  [dry-run] BORRARÍA {url_key} (sin_match)")
        time.sleep(0.1 if dry_run else 0.5)
    _print_stats(stats)

    if dry_run:
        out = RESULTS_DIR / f"{slug}_resolver_dryrun.json"
        out.write_text(json.dumps(dry_results, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        log.info(f"\n[dry-run] resultados guardados en {out} (no se escribió en Shopify)")
        _compare_dryrun_vs_snapshot(slug, url_key, dry_results)


def run_snapshot_urls(api: ShopifyAPI, vendor: str, product_id: int = None,
                      only_ids: set = None):
    """Lee los metacampos fuentes.url_fabricante y url_fabricante_2 actuales de
    cada producto del vendor (la fuente de verdad tras correcciones manuales) y:
      1. Guarda un registro auditable en resultados/{slug}_urls_snapshot.json.
      2. Siembra la caché del scraper con las url_fabricante_2 verificadas
         (si el scraper expone seed_uk_cache) → futuras resoluciones devuelven
         estas URLs por cache-hit exacto, sin volver a resolver.
    NO escribe nada en Shopify (solo lee)."""
    slug = vendor_slug(vendor)
    RESULTS_DIR.mkdir(exist_ok=True)

    def _safe_get(pid):
        try:
            return api.get_product(pid)
        except Exception as e:
            log.warning(f"  No se pudo cargar el producto {pid}: {e}")
            return None

    if product_id:
        products = [p for p in [_safe_get(product_id)] if p]
    elif only_ids:
        products = [p for p in (_safe_get(pid) for pid in only_ids) if p]
    else:
        products = api.get_products(vendor)

    snapshot = []
    title_to_url2 = {}
    n_url1 = n_url2 = 0
    for i, product in enumerate(products, 1):
        pid, title = product["id"], product["title"]
        url1 = (api.get_metafield(pid, MF_NAMESPACE, MF_KEY_URL) or {}).get("value") or ""
        url2 = (api.get_metafield(pid, MF_NAMESPACE, MF_KEY_URL_2) or {}).get("value") or ""
        snapshot.append({"id": pid, "title": title,
                         "url_fabricante": url1, "url_fabricante_2": url2})
        if url1:
            n_url1 += 1
        if url2:
            n_url2 += 1
        # Se registra siempre (incluso vacío) para que seed_uk_cache pueda
        # eliminar de la caché los productos cuyo url_fabricante_2 se borró.
        title_to_url2[title] = url2
        log.info(f"[{i}/{len(products)}] {title}  (ID: {pid})\n"
                 f"    url_fabricante  : {url1 or '—'}\n"
                 f"    url_fabricante_2: {url2 or '—'}")
        time.sleep(0.2)

    out = RESULTS_DIR / f"{slug}_urls_snapshot.json"
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    log.info(f"\nSnapshot guardado: {out} ({len(snapshot)} productos, "
             f"{n_url1} con url_fabricante, {n_url2} con url_fabricante_2)")

    # Sembrar la caché del scraper con las url_fabricante_2 verificadas.
    try:
        scraper = importlib.import_module(f"marcas.{slug}")
        if hasattr(scraper, "seed_uk_cache") and title_to_url2:
            n = scraper.seed_uk_cache(title_to_url2)
            log.info(f"Caché UK sembrada con {n} URLs verificadas "
                     f"(cache-hit exacto en futuras resoluciones)")
    except ImportError:
        pass
    except Exception as e:
        log.warning(f"No se pudo sembrar la caché: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor",          default="")
    parser.add_argument("--fuente",
                        choices=["shopify_backup", "web_oficial", "web_y_amazon"])
    parser.add_argument("--web-url",         default="")
    parser.add_argument("--product-id",      type=int, default=None)
    parser.add_argument("--only-ids",        default="")
    parser.add_argument("--rebuild-catalog", action="store_true")
    parser.add_argument("--backup",          action="store_true")
    parser.add_argument("--crear-metacampos", action="store_true",
                        help="Crear las definiciones de metacampo fuentes.* en Shopify")
    parser.add_argument("--backfill-urls",   action="store_true",
                        help="Importar URLs cacheadas a metacampos fuentes.url_fabricante")
    parser.add_argument("--resolver-urls",   action="store_true",
                        help="Buscar la URL oficial de cada producto (web) y "
                             "guardarla en fuentes.url_fabricante (sin tocar imágenes)")
    parser.add_argument("--snapshot-urls",   action="store_true",
                        help="Leer fuentes.url_fabricante(_2) actuales de Shopify y "
                             "guardarlos en resultados/{slug}_urls_snapshot.json + "
                             "sembrar la caché (no escribe en Shopify)")
    parser.add_argument("--url-key",
                        choices=["url_fabricante", "url_fabricante_2"],
                        default="url_fabricante",
                        help="Metacampo destino para --resolver-urls / --backfill-urls")
    parser.add_argument("--clear-on-no-match", action="store_true",
                        help="Si --resolver-urls no encuentra URL para un producto, "
                             "elimina el metacampo --url-key (limpia valores erróneos "
                             "de un run anterior)")
    parser.add_argument("--dry-run", action="store_true",
                        help="--resolver-urls sin escribir en Shopify: calcula qué URL "
                             "pondría, lo guarda en resultados/{slug}_resolver_dryrun.json "
                             "y, si existe el snapshot, lo compara (igual/distinto/sin_match)")
    parser.add_argument("--force-backup",    action="store_true",
                        help="Sobreescribir backups existentes")
    parser.add_argument("--pipeline",
                        choices=["standard", "webp_only"],
                        default="standard")
    parser.add_argument("--force-padding",
                        choices=["auto", "true", "false"],
                        default="auto")
    args = parser.parse_args()

    if not os.getenv("CLIENT_ID") or not os.getenv("CLIENT_SECRET"):
        log.error("Faltan CLIENT_ID / CLIENT_SECRET")
        sys.exit(1)

    if not args.crear_metacampos and not args.vendor:
        log.error("--vendor requerido")
        sys.exit(1)

    force_padding: bool | None = {"true": True, "false": False, "auto": None}[args.force_padding]

    log.info("=" * 60)
    mode = ("BACKUP" if args.backup else
            "CREAR-METACAMPOS" if args.crear_metacampos else
            "BACKFILL-URLS" if args.backfill_urls else
            "RESOLVER-URLS" if args.resolver_urls else
            "SNAPSHOT-URLS" if args.snapshot_urls else
            (args.fuente or "?").upper())
    log.info(f"  Vendor   : {args.vendor}")
    log.info(f"  Modo     : {mode}")
    log.info(f"  Pipeline : {args.pipeline}")
    log.info(f"  Padding  : {args.force_padding}")
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
        run_backup(api, args.vendor, force=args.force_backup)
    elif args.crear_metacampos:
        run_create_metafield_defs(api)
    elif args.backfill_urls:
        run_backfill_urls(api, args.vendor)
    elif args.resolver_urls:
        run_resolve_urls(api, args.vendor, args.web_url, args.rebuild_catalog,
                         args.product_id, only_ids or None, url_key=args.url_key,
                         clear_on_no_match=args.clear_on_no_match,
                         dry_run=args.dry_run)
    elif args.snapshot_urls:
        run_snapshot_urls(api, args.vendor, args.product_id, only_ids or None)
    elif args.fuente == "shopify_backup":
        run_shopify_backup(api, args.vendor, args.product_id, only_ids or None,
                           pipeline=args.pipeline, force_padding=force_padding)
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
