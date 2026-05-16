"""
Verifica que la unificación de productos AFFINITY se realizó correctamente.

Para cada grupo comprueba:
  - El producto "keep" existe y tiene las variantes esperadas
  - Los productos absorbidos ya no existen (404)
  - Los títulos son correctos
"""

import sys
sys.path.insert(0, ".")
from core.shopify_api import ShopifyAPI, get_token, _request
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Cada entrada: (grupo, keep_id, título_esperado, tamaños_esperados, absorbidos)
CHECKS = [
    (1,  15509604106627, "ADVANCE CANINE ADULT LIGHT MEDIUM POLLO (NDR)",       ["12KG","3KG"],                    [15509604204931]),
    (2,  15509604270467, "ADVANCE CANINE ADULT LIGHT MINI POLLO (NDR)",          ["1,5KG","3KG"],                   [15509604368771]),
    (3,  15509604958595, "ADVANCE CANINE ADULT MINI POLLO ARROZ (NDR)",          ["1,5KG","3KG"],                   [15509605089667]),
    (4,  15509605482883, "ADVANCE CANINE ADULT SENSITIVE SALMON ARROZ (NDR)",    ["12KG","3KG"],                    [15509605581187]),
    (5,  15509605286275, "ADVANCE CANINE ADULT SENSITIVE CORDERO ARROZ (NDR)",   ["12KG","3KG"],                    [15509605187971]),
    (6,  15509605351811, "ADVANCE CANINE ADULT SENSITIVE MINI SALMON (NDR)",     ["1,5KG","3KG"],                   [15509605417347]),
    (7,  15509605974403, "ADVANCE CANINE PUPPY MAXI POLLO ARROZ (NDR)",          ["3KG","12KG"],                    [15509605908867]),
    (8,  15509606138243, "ADVANCE CANINE PUPPY MEDIUM POLLO ARROZ (NDR)",        ["3KG","12KG"],                    [15509606072707]),
    (9,  15509606465923, "ADVANCE CANINE PUPPY SENSITIVE SALMON (NDR)",          ["3KG","12KG"],                    [15509606400387]),
    (10, 15509606793603, "ADVANCE CANINE SENIOR MINI POLLO ARROZ (NDR)",         ["1,5KG","3KG"],                   [15509606859139]),
    (11, 15509607874947, "ADVANCE FELINE ADULT STERILIZED PAVO (NDR)",           ["10KG","400GR","3KG","1,5KG"],    [15509607809411]),
    (12, 15509608137091, "ADVANCE FELINE ADULT STERILIZED SENSITIVE (NDR)",      ["10 KG","3KG","1,5KG"],           [15509608071555]),
    (13, 15509607711107, "ADVANCE FELINE ADULT STERILIZED HAIRBALL (NDR)",       ["10KG","3KG","1,5KG"],            [15509608497539]),
    (14, 15509608890755, "ADVANCE VET CANINE ADULT ARTICULAR RED. (NDR)",        ["12KG","3KG"],                    [15509608989059]),
    (15, 15509611118979, "ADVANCE VET CANINE ADULT ATOPIC CONEJO (NDR)",         ["12KG","3KG"],                    [15509609251203]),
    (16, 15509609808259, "ADVANCE VET CANINE ADULT GASTROENTERIC (NDR)",         ["3KG","800GR","12KG"],            [15509609709955]),
    (17, 15509610201475, "ADVANCE VET CANINE ADULT RENAL FAILURE (NDR)",         ["3KG","12KG"],                    [15509610135939]),
    (18, 15509610398083, "ADVANCE VET CANINE ADULT WEIGHT BALANCE (NDR)",        ["3KG","12KG"],                    [15509610529155]),
    (19, 15509612659075, "ADVANCE VET FELINE ADULT WEIGHT BALANCE (NDR)",        ["3KG","1,5KG","8KG"],             [15509612822915]),
    (20, 15509615739267, "ADVANCE MINI ADULT CHICKEN & RICE",                    ["700GR","7KG"],                   [15509615935875]),
    (21, 15509619868035, "NATURAL TRAINER CANINE ADULT MEDIUM POLLO (NDR)",      ["3KG","12KG"],                    [15509619736963]),
    (22, 15509619114371, "NATURAL TRAINER CANINE S/GLUTEN ADULT SALMON (NDR)",   ["2KG","12KG"],                    [15509619245443]),
    (23, 15509621834115, "NATURE'S VARIETY CAT NO GRAIN KITTEN POLLO (NDR)",     ["1,25KG","3KG","7KG"],            [15509621866883,15509621899651]),
    (24, 15509622161795, "NATURE'S VARIETY CAT NO GRAIN STERIL SALMÓN (NDR)",    ["7KG","3KG","1,25KG"],            [15509622096259]),
    (25, 15509621965187, "NATURE'S VARIETY CAT NO GRAIN STERIL PAVO (NDR)",      ["7KG","1,25KG","3KG"],            [15509622227331]),
    (26, 15509621440899, "NATURE'S VARIETY CAT HEALTHY GRAIN STERILIZED PESCADO",["1,25KG","300GR","3KG"],          [15509621408131]),
    (27, 15509624160643, "NATURE'S VARIETY DOG HEALTHY GRAIN ADULT MED/MAX POLLO (NDR)", ["10KG","3KG"],           [15509622489475]),
    (28, 15509622980995, "NATURE'S VARIETY DOG NO GRAIN ADULT MED/MAX POLLO (NDR)",     ["10KG","3KG"],            [15509623275907]),
    (29, 15509623013763, "NATURE'S VARIETY DOG NO GRAIN ADULT MED/MAX SALMÓN (NDR)",    ["10KG","3KG"],            [15509623308675]),
    (30, 15509623210371, "NATURE'S VARIETY DOG NO GRAIN ADULT MINI SALMÓN (NDR)",["7KG","600G","1,5KG","3KG"],     [15509623374211]),
    (31, 15509622718851, "NATURE'S VARIETY DOG HEALTHY GRAIN MINI ADULT POLLO (NDR)",["3KG","600GR","7KG","1,5KG"],[15509624127875]),
    (32, 15509618983299, "LIBRA FELINE STERILISED ATÚN (NDR)",                   ["12KG","1,5KG"],                  [15509618688387]),
    (33, 15509619048835, "LIBRA FELINE STERILISED POLLO (NDR)",                  ["12KG","1,5KG","3KG"],            [15509618622851,15509618753923]),
    ("A",15509604499843, "ADVANCE CANINE ADULT MAXI POLLO ARROZ (NDR)",          ["14KG","18KG"],                   [15509604434307]),
    ("B",15509604663683, "ADVANCE CANINE ADULT MEDIUM POLLO ARROZ (NDR)",        ["14KG","3KG","18KG"],             [15509604630915]),
    ("C",15509608694147, "ADVANCE VET CANINE ADULT ATOPIC CARE (NDR)",           ["12KG","3KG"],                    [15509609054595]),
    ("D",15509613478275, "ADVANCE VET FELINE STERILIZED URINARY LOW (NDR)",      ["1,25KG","2,5KG"],               [15509613674883]),
]


def get_product_safe(api, pid):
    try:
        r = _request("GET", f"{api.base}/products/{pid}.json", headers=api.h, timeout=20)
        return r.json().get("product")
    except Exception as e:
        if "404" in str(e) or "Not Found" in str(e):
            return None
        raise


def main():
    log.info("Obteniendo token Shopify...")
    token = get_token()
    api = ShopifyAPI(token)

    ok = []
    warn = []
    fail = []

    for entry in CHECKS:
        group, keep_id, expected_title, expected_sizes, absorbed_ids = entry

        # 1. Verificar producto keep
        product = get_product_safe(api, keep_id)
        if not product:
            fail.append(f"Grupo {group}: producto keep {keep_id} NO ENCONTRADO")
            continue

        actual_title = product["title"]
        variants = product.get("variants", [])
        actual_sizes = sorted([v["option1"] for v in variants])
        expected_sorted = sorted(expected_sizes)

        title_ok = actual_title == expected_title
        sizes_ok = set(expected_sizes).issubset(set(actual_sizes))
        n_variants = len(variants)

        if not title_ok:
            warn.append(f"Grupo {group}: título → '{actual_title}' (esperado: '{expected_title}')")
        if not sizes_ok:
            missing = set(expected_sizes) - set(actual_sizes)
            fail.append(f"Grupo {group}: faltan variantes {missing} | tiene: {actual_sizes}")

        if title_ok and sizes_ok:
            ok.append(f"Grupo {group}: ✓ '{actual_title}' | {n_variants} variantes: {actual_sizes}")

        # 2. Verificar que los absorbidos ya no existen
        for abs_id in absorbed_ids:
            absorbed = get_product_safe(api, abs_id)
            if absorbed is not None:
                fail.append(f"Grupo {group}: producto absorbido {abs_id} AÚN EXISTE ('{absorbed['title']}')")

    # ── Resumen ───────────────────────────────────────────────────────────────
    print("\n" + "="*70)
    print(f"RESULTADO: {len(ok)} OK  |  {len(warn)} AVISOS  |  {len(fail)} ERRORES")
    print("="*70)

    if ok:
        print(f"\n✓ CORRECTOS ({len(ok)}):")
        for msg in ok:
            print(f"  {msg}")

    if warn:
        print(f"\n⚠ AVISOS ({len(warn)}):")
        for msg in warn:
            print(f"  {msg}")

    if fail:
        print(f"\n✗ ERRORES ({len(fail)}):")
        for msg in fail:
            print(f"  {msg}")

    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
