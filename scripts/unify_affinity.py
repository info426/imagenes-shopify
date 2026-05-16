"""
Unifica productos AFFINITY: convierte productos separados por tamaño
en un único producto con variantes (opción "Tamaño").

Uso:
  python3 scripts/unify_affinity.py [--dry-run]

Con --dry-run muestra los cambios sin ejecutarlos.
"""

import argparse
import json
import logging
import sys
import time

sys.path.insert(0, ".")
from core.shopify_api import ShopifyAPI, get_token, _request

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── Plan de unificación ──────────────────────────────────────────────────────
#
# Cada entrada describe un grupo de productos a unificar en uno solo.
#
# keep_id      : product_id del producto que se conserva (base)
# title        : título limpio del producto unificado
# keep_size    : etiqueta de tamaño para la variante que ya existe en keep
# keep_var_id  : variant_id actual del keep_product (para renombrarla)
# absorb       : lista de variantes externas a incorporar y luego eliminar
#   product_id : producto a eliminar tras absorber su variante
#   variant_id : variant_id de ese producto
#   size        : etiqueta de tamaño para la nueva variante
#   price       : precio
#   sku         : SKU/EAN
#
# Productos que ya tienen múltiples variantes: se indica absorb con
# keep_size=None / keep_var_id=None (no renombramos opción, solo añadimos).
# ─────────────────────────────────────────────────────────────────────────────

MERGES = [
    # ── 1 ─ ADVANCE CANINE ADULT LIGHT MEDIUM POLLO ───────────────────────────
    {
        "group": 1,
        "keep_id": 15509604106627,
        "title": "ADVANCE CANINE ADULT LIGHT MEDIUM POLLO (NDR)",
        "keep_size": "12KG",
        "keep_var_id": 56973928726915,
        "absorb": [
            {"product_id": 15509604204931, "variant_id": 56973928825219,
             "size": "3KG", "price": "27.50", "sku": "8410650150406"},
        ],
    },
    # ── 2 ─ ADVANCE CANINE ADULT LIGHT MINI POLLO ─────────────────────────────
    {
        "group": 2,
        "keep_id": 15509604270467,
        "title": "ADVANCE CANINE ADULT LIGHT MINI POLLO (NDR)",
        "keep_size": "1,5KG",
        "keep_var_id": 56973928890755,
        "absorb": [
            {"product_id": 15509604368771, "variant_id": 56973928956291,
             "size": "3KG", "price": "27.50", "sku": "8410650150222"},
        ],
    },
    # ── 3 ─ ADVANCE CANINE ADULT MINI POLLO ARROZ ─────────────────────────────
    {
        "group": 3,
        "keep_id": 15509604958595,
        "title": "ADVANCE CANINE ADULT MINI POLLO ARROZ (NDR)",
        "keep_size": "1,5KG",
        "keep_var_id": 56973929578883,
        "absorb": [
            {"product_id": 15509605089667, "variant_id": 56973929709955,
             "size": "3KG", "price": "24.94", "sku": "8410650150185"},
        ],
    },
    # ── 4 ─ ADVANCE CANINE ADULT SENSITIVE SALMON ARROZ ───────────────────────
    {
        "group": 4,
        "keep_id": 15509605482883,
        "title": "ADVANCE CANINE ADULT SENSITIVE SALMON ARROZ (NDR)",
        "keep_size": "12KG",
        "keep_var_id": 56973930496387,
        "absorb": [
            {"product_id": 15509605581187, "variant_id": 56973930561923,
             "size": "3KG", "price": "27.50", "sku": "8410650150710"},
        ],
    },
    # ── 5 ─ ADVANCE CANINE ADULT SENSITIVE CORDERO ARROZ ──────────────────────
    {
        "group": 5,
        "keep_id": 15509605286275,
        "title": "ADVANCE CANINE ADULT SENSITIVE CORDERO ARROZ (NDR)",
        "keep_size": "12KG",
        "keep_var_id": 56973929906563,
        "absorb": [
            {"product_id": 15509605187971, "variant_id": 56973929808259,
             "size": "3KG", "price": "27.50", "sku": "8410650235448"},
        ],
    },
    # ── 6 ─ ADVANCE CANINE ADULT SENSITIVE MINI SALMON ────────────────────────
    {
        "group": 6,
        "keep_id": 15509605351811,
        "title": "ADVANCE CANINE ADULT SENSITIVE MINI SALMON (NDR)",
        "keep_size": "1,5KG",
        "keep_var_id": 56973929972099,
        "absorb": [
            {"product_id": 15509605417347, "variant_id": 56973930037635,
             "size": "3KG", "price": "27.50", "sku": "8410650215150"},
        ],
    },
    # ── 7 ─ ADVANCE CANINE PUPPY MAXI POLLO ARROZ ─────────────────────────────
    {
        "group": 7,
        "keep_id": 15509605974403,
        "title": "ADVANCE CANINE PUPPY MAXI POLLO ARROZ (NDR)",
        "keep_size": "3KG",
        "keep_var_id": 56973930987907,
        "absorb": [
            {"product_id": 15509605908867, "variant_id": 56973930889603,
             "size": "12KG", "price": "63.86", "sku": "8410650221502"},
        ],
    },
    # ── 8 ─ ADVANCE CANINE PUPPY MEDIUM POLLO ARROZ ───────────────────────────
    {
        "group": 8,
        "keep_id": 15509606138243,
        "title": "ADVANCE CANINE PUPPY MEDIUM POLLO ARROZ (NDR)",
        "keep_size": "3KG",
        "keep_var_id": 56973931184515,
        "absorb": [
            {"product_id": 15509606072707, "variant_id": 56973931151747,
             "size": "12KG", "price": "63.86", "sku": "8410650221625"},
        ],
    },
    # ── 9 ─ ADVANCE CANINE PUPPY SENSITIVE SALMON ─────────────────────────────
    {
        "group": 9,
        "keep_id": 15509606465923,
        "title": "ADVANCE CANINE PUPPY SENSITIVE SALMON (NDR)",
        "keep_size": "3KG",
        "keep_var_id": 56973931544963,
        "absorb": [
            {"product_id": 15509606400387, "variant_id": 56973931479427,
             "size": "12KG", "price": "68.98", "sku": "8410650009353"},
        ],
    },
    # ── 10 ─ ADVANCE CANINE SENIOR MINI POLLO ARROZ ───────────────────────────
    {
        "group": 10,
        "keep_id": 15509606793603,
        "title": "ADVANCE CANINE SENIOR MINI POLLO ARROZ (NDR)",
        "keep_size": "1,5KG",
        "keep_var_id": 56973931872643,
        "absorb": [
            {"product_id": 15509606859139, "variant_id": 56973932003715,
             "size": "3KG", "price": "27.50", "sku": "8410650150253"},
        ],
    },
    # ── 11 ─ ADVANCE FELINE ADULT STERILIZED PAVO (añadir 1,5KG) ─────────────
    # keep_id ya tiene 10KG / 400GR / 3KG → solo añadimos 1,5KG
    {
        "group": 11,
        "keep_id": 15509607874947,
        "title": "ADVANCE FELINE ADULT STERILIZED PAVO (NDR)",
        "keep_size": None,   # no renombrar opción, ya tiene variantes
        "keep_var_id": None,
        "absorb": [
            {"product_id": 15509607809411, "variant_id": 56973933248899,
             "size": "1,5KG", "price": "19.79", "sku": "8410650160474"},
        ],
    },
    # ── 12 ─ ADVANCE FELINE ADULT STERILIZED SENSITIVE (añadir 1,5KG) ─────────
    {
        "group": 12,
        "keep_id": 15509608137091,
        "title": "ADVANCE FELINE ADULT STERILIZED SENSITIVE (NDR)",
        "keep_size": None,
        "keep_var_id": None,
        "absorb": [
            {"product_id": 15509608071555, "variant_id": 56973933511043,
             "size": "1,5KG", "price": "19.79", "sku": "8410650167886"},
        ],
    },
    # ── 13 ─ ADVANCE FELINE ADULT STERILIZED HAIRBALL (añadir 1,5KG) ──────────
    {
        "group": 13,
        "keep_id": 15509607711107,
        "title": "ADVANCE FELINE ADULT STERILIZED HAIRBALL (NDR)",
        "keep_size": None,
        "keep_var_id": None,
        "absorb": [
            {"product_id": 15509608497539, "variant_id": 56973934231939,
             "size": "1,5KG", "price": "19.79", "sku": "8410650218649"},
        ],
    },
    # ── 14 ─ ADVANCE VET CANINE ADULT ARTICULAR RED. ──────────────────────────
    {
        "group": 14,
        "keep_id": 15509608890755,
        "title": "ADVANCE VET CANINE ADULT ARTICULAR RED. (NDR)",
        "keep_size": "12KG",
        "keep_var_id": 56973934625155,
        "absorb": [
            {"product_id": 15509608989059, "variant_id": 56973935018371,
             "size": "3KG", "price": "28.33", "sku": "8410650206455"},
        ],
    },
    # ── 15 ─ ADVANCE VET CANINE ADULT ATOPIC CONEJO ───────────────────────────
    {
        "group": 15,
        "keep_id": 15509611118979,
        "title": "ADVANCE VET CANINE ADULT ATOPIC CONEJO (NDR)",
        "keep_size": "12KG",
        "keep_var_id": 56973942686083,
        "absorb": [
            {"product_id": 15509609251203, "variant_id": 56973937541507,
             "size": "3KG", "price": "29.99", "sku": "8410650235257"},
        ],
    },
    # ── 16 ─ ADVANCE VET CANINE ADULT GASTROENTERIC (añadir 12KG) ─────────────
    # keep_id ya tiene 800GR / 3KG
    {
        "group": 16,
        "keep_id": 15509609808259,
        "title": "ADVANCE VET CANINE ADULT GASTROENTERIC (NDR)",
        "keep_size": None,
        "keep_var_id": None,
        "absorb": [
            {"product_id": 15509609709955, "variant_id": 56973938000259,
             "size": "12KG", "price": "75.31", "sku": "8410650167817"},
        ],
    },
    # ── 17 ─ ADVANCE VET CANINE ADULT RENAL FAILURE ───────────────────────────
    {
        "group": 17,
        "keep_id": 15509610201475,
        "title": "ADVANCE VET CANINE ADULT RENAL FAILURE (NDR)",
        "keep_size": "3KG",
        "keep_var_id": 56973938491779,
        "absorb": [
            {"product_id": 15509610135939, "variant_id": 56973938426243,
             "size": "12KG", "price": "75.31", "sku": "8410650168128"},
        ],
    },
    # ── 18 ─ ADVANCE VET CANINE ADULT WEIGHT BALANCE ──────────────────────────
    {
        "group": 18,
        "keep_id": 15509610398083,
        "title": "ADVANCE VET CANINE ADULT WEIGHT BALANCE (NDR)",
        "keep_size": "3KG",
        "keep_var_id": 56973939310979,
        "absorb": [
            {"product_id": 15509610529155, "variant_id": 56973941637507,
             "size": "12KG", "price": "75.31", "sku": "8410650168111"},
        ],
    },
    # ── 19 ─ ADVANCE VET FELINE ADULT WEIGHT BALANCE (añadir 8KG) ─────────────
    # keep_id ya tiene 1,5KG / 3KG
    {
        "group": 19,
        "keep_id": 15509612659075,
        "title": "ADVANCE VET FELINE ADULT WEIGHT BALANCE (NDR)",
        "keep_size": None,
        "keep_var_id": None,
        "absorb": [
            {"product_id": 15509612822915, "variant_id": 56973943964035,
             "size": "8KG", "price": "62.01", "sku": "8410650239163"},
        ],
    },
    # ── 20 ─ ADVANCE MINI ADULT CHICKEN & RICE ────────────────────────────────
    {
        "group": 20,
        "keep_id": 15509615739267,
        "title": "ADVANCE MINI ADULT CHICKEN & RICE",
        "keep_size": "700GR",
        "keep_var_id": 56973944947075,
        "absorb": [
            {"product_id": 15509615935875, "variant_id": 56973945012611,
             "size": "7KG", "price": "40.51", "sku": "8410650582047"},
        ],
    },
    # ── 21 ─ NATURAL TRAINER CANINE ADULT MEDIUM POLLO ────────────────────────
    {
        "group": 21,
        "keep_id": 15509619868035,
        "title": "NATURAL TRAINER CANINE ADULT MEDIUM POLLO (NDR)",
        "keep_size": "3KG",
        "keep_var_id": 56973948715395,
        "absorb": [
            {"product_id": 15509619736963, "variant_id": 56973948354947,
             "size": "12KG", "price": "48.96", "sku": "8015699006761"},
        ],
    },
    # ── 22 ─ NATURAL TRAINER CANINE S/GLUTEN ADULT SALMON ─────────────────────
    {
        "group": 22,
        "keep_id": 15509619114371,
        "title": "NATURAL TRAINER CANINE S/GLUTEN ADULT SALMON (NDR)",
        "keep_size": "2KG",
        "keep_var_id": 56973947699587,
        "absorb": [
            {"product_id": 15509619245443, "variant_id": 56973947863427,
             "size": "12KG", "price": "54.08", "sku": "8059149252537"},
        ],
    },
    # ── 23 ─ NATURE'S VARIETY CAT NO GRAIN KITTEN POLLO ──────────────────────
    {
        "group": 23,
        "keep_id": 15509621834115,
        "title": "NATURE'S VARIETY CAT NO GRAIN KITTEN POLLO (NDR)",
        "keep_size": "1,25KG",
        "keep_var_id": 56973951304067,
        "absorb": [
            {"product_id": 15509621866883, "variant_id": 56973951336835,
             "size": "3KG", "price": "30.83", "sku": "8410650597768"},
            {"product_id": 15509621899651, "variant_id": 56973951369603,
             "size": "7KG", "price": "54.26", "sku": "8410650271590"},
        ],
    },
    # ── 24 ─ NATURE'S VARIETY CAT NO GRAIN STERIL SALMÓN (añadir 1,25KG) ─────
    # keep_id ya tiene 3KG / 7KG
    {
        "group": 24,
        "keep_id": 15509622161795,
        "title": "NATURE'S VARIETY CAT NO GRAIN STERIL SALMÓN (NDR)",
        "keep_size": None,
        "keep_var_id": None,
        "absorb": [
            {"product_id": 15509622096259, "variant_id": 56973951566211,
             "size": "1,25KG", "price": "19.02", "sku": "8410650271811"},
        ],
    },
    # ── 25 ─ NATURE'S VARIETY CAT NO GRAIN STERIL PAVO (añadir 3KG) ──────────
    # keep_id ya tiene 1,25KG / 7KG
    {
        "group": 25,
        "keep_id": 15509621965187,
        "title": "NATURE'S VARIETY CAT NO GRAIN STERIL PAVO (NDR)",
        "keep_size": None,
        "keep_var_id": None,
        "absorb": [
            {"product_id": 15509622227331, "variant_id": 56973951697283,
             "size": "3KG", "price": "29.93", "sku": "8410650597775"},
        ],
    },
    # ── 26 ─ NATURE'S VARIETY CAT HG STERILIZED PESCADO (FISH = PESCADO) ─────
    # keep_id ya tiene 300GR / 1,25KG → añadir 3KG (producto en inglés "FISH")
    {
        "group": 26,
        "keep_id": 15509621440899,
        "title": "NATURE'S VARIETY CAT HEALTHY GRAIN STERILIZED PESCADO",
        "keep_size": None,
        "keep_var_id": None,
        "absorb": [
            {"product_id": 15509621408131, "variant_id": 56973950878083,
             "size": "3KG", "price": "30.83", "sku": "8410650597812"},
        ],
    },
    # ── 27 ─ NATURE'S VARIETY DOG HG ADULT MED/MAX POLLO ─────────────────────
    {
        "group": 27,
        "keep_id": 15509624160643,
        "title": "NATURE'S VARIETY DOG HEALTHY GRAIN ADULT MED/MAX POLLO (NDR)",
        "keep_size": "10KG",
        "keep_var_id": 56973953892739,
        "absorb": [
            {"product_id": 15509622489475, "variant_id": 56973951992195,
             "size": "3KG", "price": "25.71", "sku": "8410650597836"},
        ],
    },
    # ── 28 ─ NATURE'S VARIETY DOG NG ADULT MED/MAX POLLO ─────────────────────
    {
        "group": 28,
        "keep_id": 15509622980995,
        "title": "NATURE'S VARIETY DOG NO GRAIN ADULT MED/MAX POLLO (NDR)",
        "keep_size": "10KG",
        "keep_var_id": 56973952483715,
        "absorb": [
            {"product_id": 15509623275907, "variant_id": 56973953040771,
             "size": "3KG", "price": "27.37", "sku": "8410650597829"},
        ],
    },
    # ── 29 ─ NATURE'S VARIETY DOG NG ADULT MED/MAX SALMÓN ────────────────────
    {
        "group": 29,
        "keep_id": 15509623013763,
        "title": "NATURE'S VARIETY DOG NO GRAIN ADULT MED/MAX SALMÓN (NDR)",
        "keep_size": "10KG",
        "keep_var_id": 56973952516483,
        "absorb": [
            {"product_id": 15509623308675, "variant_id": 56973953073539,
             "size": "3KG", "price": "29.10", "sku": "8410650597874"},
        ],
    },
    # ── 30 ─ NATURE'S VARIETY DOG NG ADULT MINI SALMÓN (añadir 3KG) ──────────
    # keep_id ya tiene 600G / 1,5KG / 7KG
    {
        "group": 30,
        "keep_id": 15509623210371,
        "title": "NATURE'S VARIETY DOG NO GRAIN ADULT MINI SALMÓN (NDR)",
        "keep_size": None,
        "keep_var_id": None,
        "absorb": [
            {"product_id": 15509623374211, "variant_id": 56973953139075,
             "size": "3KG", "price": "29.10", "sku": "8410650595962"},
        ],
    },
    # ── 31 ─ NATURE'S VARIETY DOG HG MINI ADULT POLLO (unir 2 productos) ─────
    # keep_id tiene 600GR / 3KG → absorber el que tiene 1,5KG / 7KG
    {
        "group": 31,
        "keep_id": 15509622718851,
        "title": "NATURE'S VARIETY DOG HEALTHY GRAIN MINI ADULT POLLO (NDR)",
        "keep_size": None,
        "keep_var_id": None,
        "absorb": [
            {"product_id": 15509624127875, "variant_id": 56973953859971,
             "size": "7KG", "price": "45.50", "sku": "8410650271200"},
            {"product_id": 15509624127875, "variant_id": 57830741475715,
             "size": "1,5KG", "price": "16.44", "sku": "8410650271613"},
        ],
    },
    # ── 32 ─ LIBRA FELINE STERILISED ATÚN ────────────────────────────────────
    {
        "group": 32,
        "keep_id": 15509618983299,
        "title": "LIBRA FELINE STERILISED ATÚN (NDR)",
        "keep_size": "12KG",
        "keep_var_id": 56973947568515,
        "absorb": [
            {"product_id": 15509618688387, "variant_id": 56973947306371,
             "size": "1,5KG", "price": "12.56", "sku": "8410650262307"},
        ],
    },
    # ── 33 ─ LIBRA FELINE STERILISED POLLO ───────────────────────────────────
    # absorber el sin sabor (1,5KG pollo) + el 3KG, manteniendo el 12KG
    {
        "group": 33,
        "keep_id": 15509619048835,
        "title": "LIBRA FELINE STERILISED POLLO (NDR)",
        "keep_size": "12KG",
        "keep_var_id": 56973947634051,
        "absorb": [
            {"product_id": 15509618622851, "variant_id": 56973947240835,
             "size": "1,5KG", "price": "12.56", "sku": "8410650203072"},
            {"product_id": 15509618753923, "variant_id": 56973947371907,
             "size": "3KG", "price": "17.40", "sku": "8410650233543"},
        ],
    },
    # ── A ─ ADVANCE CANINE ADULT MAXI POLLO ARROZ (MAX 18KG + MAXI 14KG) ─────
    {
        "group": "A",
        "keep_id": 15509604499843,
        "title": "ADVANCE CANINE ADULT MAXI POLLO ARROZ (NDR)",
        "keep_size": "14KG",
        "keep_var_id": 56973929120131,
        "absorb": [
            {"product_id": 15509604434307, "variant_id": 56973929054595,
             "size": "18KG", "price": "79.47", "sku": "8410650221588"},
        ],
    },
    # ── B ─ ADVANCE CANINE ADULT MEDIUM POLLO ARROZ (añadir 18KG online) ──────
    # keep_id ya tiene 3KG / 14KG
    {
        "group": "B",
        "keep_id": 15509604663683,
        "title": "ADVANCE CANINE ADULT MEDIUM POLLO ARROZ (NDR)",
        "keep_size": None,
        "keep_var_id": None,
        "absorb": [
            {"product_id": 15509604630915, "variant_id": 56973929251203,
             "size": "18KG", "price": "79.47", "sku": "8410650221571"},
        ],
    },
    # ── C ─ ADVANCE VET CANINE ADULT ATOPIC CARE ──────────────────────────────
    {
        "group": "C",
        "keep_id": 15509608694147,
        "title": "ADVANCE VET CANINE ADULT ATOPIC CARE (NDR)",
        "keep_size": "12KG",
        "keep_var_id": 56973934428547,
        "absorb": [
            {"product_id": 15509609054595, "variant_id": 56973935083907,
             "size": "3KG", "price": "29.99", "sku": "8410650170695"},
        ],
    },
    # ── D ─ ADVANCE VET FELINE STERILIZED URINARY LOW ─────────────────────────
    {
        "group": "D",
        "keep_id": 15509613478275,
        "title": "ADVANCE VET FELINE STERILIZED URINARY LOW (NDR)",
        "keep_size": "1,25KG",
        "keep_var_id": 56973944160643,
        "absorb": [
            {"product_id": 15509613674883, "variant_id": 56973944226179,
             "size": "2,5KG", "price": "32.10", "sku": "8410650239859"},
        ],
    },
]


# ─── Lógica de unificación ────────────────────────────────────────────────────

OPTION_NAME = "Tamaño"


def _base(api: ShopifyAPI):
    return f"https://{api.base.split('/admin')[0].split('https://')[1]}/admin/api/2024-10"


def update_product(api: ShopifyAPI, product_id: int, payload: dict, dry: bool):
    url = f"{api.base}/products/{product_id}.json"
    if dry:
        log.info(f"    [DRY] PUT {url} → {json.dumps(payload)[:120]}")
        return {}
    r = _request("PUT", url, headers=api.h, json={"product": payload}, timeout=30)
    return r.json().get("product", {})


def add_variant(api: ShopifyAPI, product_id: int, variant: dict, dry: bool):
    url = f"{api.base}/products/{product_id}/variants.json"
    if dry:
        log.info(f"    [DRY] POST {url} → {json.dumps(variant)[:120]}")
        return {}
    r = _request("POST", url, headers=api.h, json={"variant": variant}, timeout=30)
    return r.json().get("variant", {})


def delete_product(api: ShopifyAPI, product_id: int, dry: bool):
    url = f"{api.base}/products/{product_id}.json"
    if dry:
        log.info(f"    [DRY] DELETE {url}")
        return
    _request("DELETE", url, headers=api.h, timeout=30)


def process_merge(api: ShopifyAPI, merge: dict, dry: bool):
    g = merge["group"]
    keep_id = merge["keep_id"]
    title = merge["title"]
    keep_size = merge["keep_size"]
    keep_var_id = merge["keep_var_id"]

    log.info(f"\n{'─'*60}")
    log.info(f"Grupo {g}: {title}")

    # 1. Si el producto base tiene una sola variante "Default Title",
    #    actualizamos la opción a "Tamaño" y renombramos esa variante.
    if keep_size and keep_var_id:
        log.info(f"  → Renombrando opción a '{OPTION_NAME}', variante base = {keep_size}")
        update_product(api, keep_id, {
            "id": keep_id,
            "title": title,
            "options": [{"name": OPTION_NAME}],
        }, dry)
        time.sleep(0.5)
        # Actualizar option1 de la variante existente
        url = f"{api.base}/variants/{keep_var_id}.json"
        payload = {"variant": {"id": keep_var_id, "option1": keep_size}}
        if dry:
            log.info(f"    [DRY] PUT {url} option1={keep_size}")
        else:
            _request("PUT", url, headers=api.h, json=payload, timeout=30)
        time.sleep(0.5)
    else:
        # Solo actualizar título si cambió
        log.info(f"  → Actualizando título: {title}")
        update_product(api, keep_id, {"id": keep_id, "title": title}, dry)
        time.sleep(0.5)

    # Conjunto de product_ids a eliminar (agrupamos para no borrar dos veces
    # si un mismo producto aporta varias variantes, como grupo 31)
    products_to_delete = {}

    # 2. Añadir variantes absorbidas
    for item in merge["absorb"]:
        abs_pid = item["product_id"]
        size = item["size"]
        log.info(f"  + Añadiendo variante {size} (SKU {item['sku']}) desde producto {abs_pid}")
        add_variant(api, keep_id, {
            "option1": size,
            "price": item["price"],
            "sku": item["sku"],
            "inventory_management": None,
        }, dry)
        time.sleep(0.5)
        products_to_delete[abs_pid] = True

    # 3. Eliminar productos absorbidos
    for abs_pid in products_to_delete:
        log.info(f"  ✗ Eliminando producto {abs_pid}")
        delete_product(api, abs_pid, dry)
        time.sleep(0.5)

    log.info(f"  ✓ Grupo {g} completado")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Muestra los cambios sin ejecutarlos")
    parser.add_argument("--group", type=str, default=None,
                        help="Procesar solo el grupo indicado (ej: 1, A)")
    args = parser.parse_args()

    log.info("Obteniendo token Shopify...")
    token = get_token()
    api = ShopifyAPI(token)

    merges = MERGES
    if args.group:
        target = args.group if args.group.isalpha() else int(args.group)
        merges = [m for m in MERGES if str(m["group"]) == str(target)]
        if not merges:
            log.error(f"Grupo '{args.group}' no encontrado")
            sys.exit(1)

    log.info(f"{'[DRY RUN] ' if args.dry_run else ''}Procesando {len(merges)} grupos...")

    errors = []
    for merge in merges:
        try:
            process_merge(api, merge, args.dry_run)
        except Exception as e:
            log.error(f"  ERROR en grupo {merge['group']}: {e}")
            errors.append((merge["group"], str(e)))

    log.info(f"\n{'='*60}")
    log.info(f"Completado: {len(merges) - len(errors)}/{len(merges)} grupos OK")
    if errors:
        log.error(f"Errores: {errors}")
        sys.exit(1)


if __name__ == "__main__":
    main()
