#!/usr/bin/env python3
"""
Asigna la imagen correcta a cada variante de un producto Shopify
leyendo el texto de las imágenes con OCR (Tesseract).

Flujo:
  1. Obtiene variantes e imágenes del producto vía Shopify API
  2. Descarga cada imagen y extrae texto con pytesseract
  3. Normaliza tokens del texto y del título de cada variante
  4. Asigna a cada variante la imagen con mayor coincidencia (Jaccard)
  5. Si OCR no discrimina (todos scores=0), fallback por orden de posición
  6. Actualiza la variante en Shopify con el image_id correspondiente
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
from PIL import Image, ImageEnhance, ImageFilter

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
    # Normalizar coma decimal → punto: "1,5" → "1.5"
    text = re.sub(r"(\d),(\d)", r"\1.\2", text)
    # Separar números pegados a letras (preservando decimales)
    text = re.sub(r"(\d)([a-z])", r"\1 \2", text)
    text = re.sub(r"([a-z])(\d)", r"\1 \2", text)
    # Extraer tokens: decimales como un solo token (ej: "1.5"), enteros y palabras
    tokens = set(re.findall(r"\d+\.\d+|\d+|[a-z]+", text))
    return tokens - STOPWORDS


def weight_score(variant_title: str, raw_text: str) -> float:
    """
    Busca el peso del título de variante en el texto OCR crudo.
    Retorna 1.0 si encuentra número+unidad ("5 kg", "1,5 kg"),
    0.3 si encuentra el número solo como token aislado.
    """
    raw = unicodedata.normalize("NFD", raw_text.lower()).encode("ascii", "ignore").decode()
    m = re.search(r"(\d+[.,]\d+|\d+)\s*(?:kg|kilo)", variant_title, re.IGNORECASE)
    if not m:
        return 0.0
    num = m.group(1).replace(",", ".")
    # Patrón que acepta punto o coma como separador decimal
    num_pat = re.escape(num).replace(r"\.", r"[.,]")
    if re.search(rf"\b{num_pat}\s*(?:kg|kilo|lb)\b", raw, re.IGNORECASE):
        return 1.0
    if re.search(rf"\b{num_pat}\b", raw):
        return 0.3
    return 0.0


def preprocess_for_ocr(img: Image.Image) -> Image.Image:
    """Convierte a escala de grises y mejora contraste/nitidez para OCR."""
    gray = img.convert("L")
    # Trabajar siempre a resolución completa (no reducir; ampliar si es pequeña)
    min_ocr_dim = 2000
    if max(gray.size) < min_ocr_dim:
        ratio = min_ocr_dim / max(gray.size)
        gray = gray.resize(
            (int(gray.width * ratio), int(gray.height * ratio)), Image.LANCZOS
        )
    gray = ImageEnhance.Contrast(gray).enhance(2.0)
    gray = gray.filter(ImageFilter.SHARPEN)
    return gray


def ocr_image(img: Image.Image) -> str:
    """Extrae texto combinando múltiples configs de Tesseract (PSM 3, 6, 11)."""
    try:
        import pytesseract
    except ImportError:
        log.error("pytesseract no instalado. Ejecuta: pip install pytesseract")
        sys.exit(1)

    gray = preprocess_for_ocr(img)
    parts = []
    for psm in (3, 6, 11):
        config = f"--psm {psm} --oem 3"
        text = pytesseract.image_to_string(gray, config=config, lang="eng+spa")
        parts.append(text)
        log.debug(f"    PSM {psm}: {text[:120]!r}")
    return "\n".join(parts)


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def match_images_to_variants(variants: list, images_ocr: list) -> dict:
    """
    Devuelve {variant_id: (image_id, image_pos, score)}.
    score = -1 indica asignación por fallback de posición.
    """
    assignments = {}

    for variant in variants:
        vid   = variant["id"]
        title = variant["title"]
        vtoks = normalize(title)
        log.info(f"\n  Variante: «{title}» → tokens: {vtoks}")

        best_score = -1.0
        best_img   = None
        for img in images_ocr:
            w = weight_score(title, img["raw_text"])
            j = jaccard(vtoks, img["tokens"])
            # El score de peso domina sobre Jaccard (×5)
            score = w * 5 + j
            log.info(f"    img pos{img['position']}: score={score:.3f}  (weight={w:.1f} jaccard={j:.3f})")
            if score > best_score:
                best_score = score
                best_img   = img

        if best_img and best_score > 0:
            assignments[vid] = (best_img["id"], best_img["position"], best_score)
            log.info(f"  → img pos{best_img['position']} (score={best_score:.3f})")
        else:
            log.warning(f"  → Sin match para variante «{title}»")

    # Detectar si OCR no discriminó: todos score=0, alguna variante sin match,
    # o varias variantes asignadas a la misma imagen (colisión)
    assigned_images = [img_id for img_id, _, _ in assignments.values()]
    collision = len(assigned_images) != len(set(assigned_images))
    all_zero  = all(score == 0 for _, _, score in assignments.values())
    missing   = len(assignments) < len(variants)

    if (all_zero or missing or collision) and len(images_ocr) >= len(variants):
        reason = ("colisión" if collision else
                  "scores=0" if all_zero else "variante sin match")
        log.warning(
            f"\n  ⚠ OCR no discriminativo ({reason}) — "
            "usando fallback por orden de posición"
        )
        sorted_imgs = sorted(images_ocr, key=lambda x: x["position"])
        for i, variant in enumerate(variants):
            vid = variant["id"]
            img = sorted_imgs[i]
            assignments[vid] = (img["id"], img["position"], -1.0)
            log.info(
                f"  [FALLBACK] «{variant['title']}» → img pos{img['position']}"
            )

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
            r        = requests.get(url, headers=HEADERS, timeout=60)
            r.raise_for_status()
            img      = Image.open(BytesIO(r.content))
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

    # Resumen y actualización
    log.info("\n  ── Resultado ──")
    result = []
    for variant in variants:
        vid    = variant["id"]
        vtitle = variant["title"]
        if vid in assignments:
            img_id, img_pos, score = assignments[vid]
            is_fallback = score < 0
            score_str   = "pos-order" if is_fallback else f"{score:.3f}"
            if dry_run:
                mode = "FALLBACK→DRY-RUN" if is_fallback else "DRY-RUN"
                log.info(f"  [{mode}] «{vtitle}» → imagen pos{img_pos} (score={score_str})")
            else:
                mode = "FALLBACK→ASIGNADO" if is_fallback else "ASIGNADO"
                log.info(f"  [{mode}] «{vtitle}» → imagen pos{img_pos} (image_id={img_id})")
                try:
                    resp = api.set_variant_image(vid, img_id)
                    assigned = resp.get("variant", {}).get("image_id")
                    log.info(f"    ✓ API OK — variant.image_id={assigned}")
                except Exception as e:
                    log.error(f"    ✗ API ERROR: {e}")
                    raise
            result.append({"variant_id": vid, "variant": vtitle,
                           "image_id": img_id, "image_pos": img_pos,
                           "score": score, "fallback": is_fallback})
        else:
            log.warning(f"  [SIN MATCH] «{vtitle}»")
            result.append({"variant_id": vid, "variant": vtitle,
                           "image_id": None, "image_pos": None,
                           "score": 0, "fallback": False})

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
