"""
Unifica productos ARTERO en Shopify.

Grupos a unificar:
  G1 — CORREA DOG CONTROL 360: XS / S / M  (3 → 1, opción Talla)
  G2 — CORREA PELUQUERIA:  Amarillo / Azul / Rosa  (3 → 1, opción Color)
  G4 — CHAMPU HIDRATANTE:  Estándar / 5 Litros     (2 → 1, opción Tamaño)
  G5 — CHAMPU BLANC:       Estándar / 5 Litros     (2 → 1, opción Tamaño)
  G6 — CHAMPU VITALIZANTE: Estándar / 100ML        (2 → 1, opción Tamaño)
  G8 — CHAMPU DETOX CARBON ACTIVO: Estándar / 100ML (2 → 1, opción Tamaño)
"""

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.shopify_api import ShopifyAPI, get_token, _request


# ---------------------------------------------------------------------------
# Definición de grupos
# ---------------------------------------------------------------------------

GRUPOS = [
    {
        "id": "G1",
        "desc": "CORREA DOG CONTROL 360",
        "title": "ARTERO CORREA DOG CONTROL 360",
        "option": "Talla",
        "base_id": 15509636809091,   # XS 12KG
        "base_label": "XS",
        "secondaries": [
            {"id": 15509636940163, "label": "S"},    # S 15KG
            {"id": 15509636874627, "label": "M"},    # M 25KG
        ],
    },
    {
        "id": "G2",
        "desc": "CORREA PELUQUERIA 50CM",
        "title": "ARTERO CORREA PELUQUERIA 50CM",
        "option": "Color",
        "base_id": 15509636972931,   # Amarillo
        "base_label": "Amarillo",
        "secondaries": [
            {"id": 15509637005699, "label": "Azul"},
            {"id": 15509637104003, "label": "Rosa"},
        ],
    },
    {
        "id": "G4",
        "desc": "CHAMPU HIDRATANTE",
        "title": "ARTERO CHAMPU HIDRATANTE",
        "option": "Tamaño",
        "base_id": 15509636284803,   # estándar
        "base_label": "Estándar",
        "secondaries": [
            {"id": 15509636383107, "label": "5 Litros"},
        ],
    },
    {
        "id": "G5",
        "desc": "CHAMPU BLANC",
        "title": "ARTERO CHAMPU BLANC",
        "option": "Tamaño",
        "base_id": 15509635727747,   # estándar
        "base_label": "Estándar",
        "secondaries": [
            {"id": 15509635793283, "label": "5 Litros"},
        ],
    },
    {
        "id": "G6",
        "desc": "CHAMPU VITALIZANTE",
        "title": "ARTERO CHAMPU VITALIZANTE",
        "option": "Tamaño",
        "base_id": 15509636776323,   # estándar
        "base_label": "Estándar",
        "secondaries": [
            {"id": 15509636645251, "label": "100ML"},
        ],
    },
    {
        "id": "G8",
        "desc": "CHAMPU DETOX CARBON ACTIVO",
        "title": "ARTERO CHAMPU DETOX CARBON ACTIVO",
        "option": "Tamaño",
        "base_id": 15509636088195,   # Carbon Activo (estándar)
        "base_label": "Estándar",
        "secondaries": [
            {"id": 15509635957123, "label": "100ML"},
        ],
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_product(api, pid):
    r = _request("GET", f"{api.base}/products/{pid}.json", headers=api.h, timeout=30)
    return r.json()["product"]


def update_product(api, pid, payload):
    r = _request("PUT", f"{api.base}/products/{pid}.json",
                 headers=api.h, json={"product": payload}, timeout=30)
    return r.json()["product"]


def add_variant(api, pid, variant_payload):
    r = _request("POST", f"{api.base}/products/{pid}/variants.json",
                 headers=api.h, json={"variant": variant_payload}, timeout=30)
    return r.json()["variant"]


def delete_product(api, pid):
    _request("DELETE", f"{api.base}/products/{pid}.json", headers=api.h, timeout=30)


def get_images(api, pid):
    r = _request("GET", f"{api.base}/products/{pid}/images.json", headers=api.h, timeout=30)
    return r.json().get("images", [])


def copy_images(api, src_pid, dst_pid, variant_ids=None):
    """Copia todas las imágenes de src_pid a dst_pid, asignadas a variant_ids."""
    images = get_images(api, src_pid)
    copied = []
    for img in images:
        payload = {"src": img["src"].split("?")[0], "alt": img.get("alt") or ""}
        if variant_ids:
            payload["variant_ids"] = variant_ids
        r = _request("POST", f"{api.base}/products/{dst_pid}/images.json",
                     headers=api.h, json={"image": payload}, timeout=30)
        new_img = r.json().get("image", {})
        copied.append(new_img.get("id"))
        pause()
    return copied


def assign_variant_image(api, variant_id, image_id):
    _request("PUT", f"{api.base}/variants/{variant_id}.json",
             headers=api.h,
             json={"variant": {"id": variant_id, "image_id": image_id}},
             timeout=30)


def pause():
    time.sleep(0.5)


# ---------------------------------------------------------------------------
# Lógica de unificación
# ---------------------------------------------------------------------------

def unify_group(api, group, dry_run=False):
    gid    = group["id"]
    title  = group["title"]
    option = group["option"]
    base_id     = group["base_id"]
    base_label  = group["base_label"]
    secondaries = group["secondaries"]

    print(f"\n{'='*60}")
    print(f"[{gid}] {group['desc']}")
    print(f"{'='*60}")

    if dry_run:
        print(f"  Título unificado : '{title}'")
        print(f"  Opción           : '{option}'")
        print(f"  Base             : {base_id} → variante '{base_label}'")
        for s in secondaries:
            print(f"  Fusionar         : {s['id']} → variante '{s['label']}'")
        return

    # — Paso 1: actualizar producto base (título, opción, label de variante base)
    base = get_product(api, base_id)
    base_variant = base["variants"][0]
    update_product(api, base_id, {
        "id":    base_id,
        "title": title,
        "options": [{"name": option}],
        "variants": [{
            "id":      base_variant["id"],
            "option1": base_label,
            "price":   base_variant["price"],
            "barcode": base_variant.get("barcode"),
            "sku":     base_variant.get("sku") or base_variant.get("barcode"),
        }],
    })
    pause()
    print(f"  ✓ base actualizado: '{title}', variante '{base_label}'")

    # Asignar primera imagen del base a su variante (para selector de imágenes)
    imgs_base = get_images(api, base_id)
    if imgs_base:
        assign_variant_image(api, base_variant["id"], imgs_base[0]["id"])
        pause()
        print(f"  ✓ imagen base asignada a variante '{base_label}'")

    # — Paso 2: para cada producto secundario, añadir variante, copiar imágenes, eliminar
    for sec in secondaries:
        sec_product = get_product(api, sec["id"])
        sec_variant = sec_product["variants"][0]
        pause()

        new_var = add_variant(api, base_id, {
            "option1":     sec["label"],
            "price":       sec_variant["price"],
            "barcode":     sec_variant.get("barcode"),
            "sku":         sec_variant.get("sku") or sec_variant.get("barcode"),
            "weight":      sec_variant.get("weight") or 0,
            "weight_unit": sec_variant.get("weight_unit") or "kg",
        })
        pause()
        print(f"  ✓ variante añadida: '{sec['label']}' (id={new_var['id']})")

        copied = copy_images(api, sec["id"], base_id, variant_ids=[new_var["id"]])
        print(f"  ✓ {len(copied)} imagen(es) copiada(s) de {sec['id']} ({sec_product['title']})")

        delete_product(api, sec["id"])
        pause()
        print(f"  ✓ producto eliminado: {sec['id']} ({sec_product['title']})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    dry_run    = "--dry-run" in sys.argv
    only_group = None
    if "--group" in sys.argv:
        idx = sys.argv.index("--group")
        only_group = sys.argv[idx + 1].upper() if idx + 1 < len(sys.argv) else None

    grupos = GRUPOS
    if only_group:
        grupos = [g for g in GRUPOS if g["id"] == only_group]
        if not grupos:
            print(f"Grupo '{only_group}' no encontrado. Disponibles: {[g['id'] for g in GRUPOS]}")
            sys.exit(1)

    if dry_run:
        print("=== DRY RUN — no se realizarán cambios en Shopify ===")

    if not dry_run:
        token = get_token()
        api   = ShopifyAPI(token)
    else:
        api = None

    for group in grupos:
        try:
            unify_group(api, group, dry_run=dry_run)
        except Exception as e:
            print(f"  ✗ ERROR en {group['id']}: {e}")

    print(f"\n{'='*60}")
    print("✓ Proceso completado")


if __name__ == "__main__":
    main()
