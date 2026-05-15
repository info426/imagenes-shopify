#!/usr/bin/env python3
"""
Asigna la imagen correcta a cada variante de un producto Shopify
leyendo el texto de las imágenes con OCR (Tesseract).

Flujo:
  1. Obtiene variantes e imágenes del producto vía Shopify API
  2. Descarga cada imagen y extrae texto con pytesseract
  3. Normaliza tokens del texto y del título de cada variante
  4. Asigna a cada variante la imagen con mayor coincidencia (Jaccard)
  5. Actualiza la variante en Shopify con el image_id correspondiente
"""

import argparse
import json
import logging
import re
import sys
import unicodedata
from io import BytesIO
from pathlib import Path

import requests
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.shopify_api import ShopifyAPI, get_token

RESULTS_DIR = Path("resultados")
HEADERS = {"User-Agent": "Mozilla/5.0"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# Stopwords que no ayudan a discriminar variantes
STOPWORDS = {"de", "para", "con", "and", "for", "with", "gr", "g", "kg",
             "ml", "l", "x", "pcs", "ud", "uds", "pack", "caja"}


def normalize(text: str) -> set:
    """Convierte texto a tokens normalizados para comparación."""
    text = unicodedata.normalize("NFD", text.lower()).encode("ascii", "ignore").decode()
    # Separar números pegados a letras: "15x60gr" → "15 x 60 gr"
    text = re.sub(r"(\d)([a-z])", r"\1 \2", text)
    text = re.sub(r"([a-z])(\d)", r"\1 \2", text)
    tokens = set(re.findall(r"[a-z0-9]+", text))
    return tokens - STOPWORDS


def ocr_image(img: Image.Image) -> str:
    """Extrae texto de una imagen con Tesseract."""
    try:
        import pytesseract
    except ImportError:
        log.error("pytesseract no instalado. Ejecuta: pip install pytesseract")
        sys.exit(1)

    # Procesar en escala de grises para mejor OCR
    gray = img.convert("L")

    # Si la imagen es muy grande, redimensionar para acelerar OCR
    max_dim = 1500
    if max(gray.size) > max_dim:
        ratio = max_dim / max(gray.size)
        gray = gray.resize((int(gray.width * ratio), int(gray.height * ratio)), Image.LANCZOS)

    # PSM 11: texto disperso (bueno para packaging)
    config = "--psm 11 --oem 3"
    text = pytesseract.image_to_string(gray, config=config, lang="eng+spa")
    return text


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def match_images_to_variants(variants: list, images_ocr: list) -> dict:
    """
    Devuelve {variant_id: image_id} con la mejor asignación.
    images_ocr: [{id, position, src, tokens}]
    """
    assignments = {}

    for variant in variants:
        vid   = variant["id"]
        title = variant["title"]
        vtoks = normalize(title)
        log.info(f"\n  Variante: «{title}» → tokens: {vtoks}")

        best_score = -1
        best_img   = None
        for img in images_ocr:
            score = jaccard(vtoks, img["tokens"])
            log.info(f"    img pos{img['position']}: score={score:.3f}  tokens={img['tokens']}")
            if score > best_score:
                best_score = score
                best_img   = img

        if best_img and best_score > 0:
            assignments[vid] = (best_img["id"], best_img["position"], best_score)
            log.info(f"  → img pos{best_img['position']} (score={best_score:.3f})")
        else:
            log.warning(f"  → Sin match para variante «{title}»")

    return assignments


def run(api: ShopifyAPI, product_id: int, dry_run: bool):
    product  = api.get_product(product_id)
    title    = product["title"]
    variants = product.get("variants", [])
    images   = api.get_images(product_id)

    log.info(f"\n[{product_id}] {title}")
    log.info(f"  {len(variants)} variante(s), {len(images)} imagen(es)")

    if not variants:
        log.warning("  Sin variantes — nada que hacer")
        return
    if not images:
        log.warning("  Sin imágenes — nada que hacer")
        return

    # OCR de cada imagen
    images_ocr = []
    for img_data in sorted(images, key=lambda x: x.get("position", 999)):
        url = img_data["src"].split("?")[0]
        pos = img_data.get("position", "?")
        log.info(f"\n  Analizando imagen pos{pos}: {url.split('/')[-1]}")
        try:
            r   = requests.get(url, headers=HEADERS, timeout=60)
            r.raise_for_status()
            img = Image.open(BytesIO(r.content))
            raw_text = ocr_image(img)
            tokens   = normalize(raw_text)
            log.info(f"    OCR tokens: {tokens}")
            images_ocr.append({
                "id":       img_data["id"],
                "position": pos,
                "src":      url,
                "tokens":   tokens,
                "raw_text": raw_text,
            })
        except Exception as e:
            log.warning(f"    Error en img pos{pos}: {e}")

    if not images_ocr:
        log.error("  No se pudo procesar ninguna imagen con OCR")
        return

    # Matching variante → imagen
    log.info("\n  ── Matching ──")
    assignments = match_images_to_variants(variants, images_ocr)

    # Resumen
    log.info("\n  ── Resultado ──")
    result = []
    for variant in variants:
        vid   = variant["id"]
        vtitle = variant["title"]
        if vid in assignments:
            img_id, img_pos, score = assignments[vid]
            status = "DRY-RUN" if dry_run else "ASIGNADO"
            log.info(f"  [{status}] «{vtitle}» → imagen pos{img_pos} (score={score:.3f})")
            if not dry_run:
                api.set_variant_image(vid, img_id)
            result.append({"variant_id": vid, "variant": vtitle,
                           "image_id": img_id, "image_pos": img_pos, "score": score})
        else:
            log.warning(f"  [SIN MATCH] «{vtitle}»")
            result.append({"variant_id": vid, "variant": vtitle,
                           "image_id": None, "image_pos": None, "score": 0})

    # Guardar resultado
    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"variant_images_{product_id}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    log.info(f"\n  Log guardado: {out}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-id", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="Solo muestra el matching, no actualiza Shopify")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info(f"  Producto : {args.product_id}")
    log.info(f"  Modo     : {'DRY-RUN' if args.dry_run else 'APLICAR'}")
    log.info("=" * 60)

    token = get_token()
    api   = ShopifyAPI(token)
    run(api, args.product_id, args.dry_run)
    log.info("\n✓ Completado")


if __name__ == "__main__":
    main()
