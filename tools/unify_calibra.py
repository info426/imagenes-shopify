"""
Unifica productos CALIBRA en Shopify:
  Grupo A — añade el bundle 12+2KG como variante del producto base y elimina el producto bundle
  Grupo B — une el 80g y el 250g en un único producto con variante Peso, elimina el duplicado
"""

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.shopify_api import ShopifyAPI, get_token, _request

# ---------------------------------------------------------------------------
# Configuración de grupos
# ---------------------------------------------------------------------------

GRUPO_A = [
    # (base_id, bundle_id, bundle_label)
    # base ya tiene opción Peso con variantes existentes
    (15509679833475, 15509679800707, "12+2KG"),   # PREMIUM LINE ADULT POLLO       (tiene 12KG+3KG)
    (15509680357763, 15509680324995, "12+2KG"),   # PREMIUM LINE SENIOR LIGHT POLLO (tiene 12KG+3KG)
    (15509676949891, 15509677015427, "12+2KG"),   # LIFE ADULT MEDIUM BREED POLLO   (tiene 12KG+2,5KG)
    (15509675868547, 15509675966851, "12+2KG"),   # EXPERT NUTRITION SENSITIVE SALMON (tiene 12KG+2KG)
    # base tiene sólo Title/Default Title → hay que convertir a Peso primero
    (15509679735171, 15509679636867, "12+2KG"),   # PREMIUM LINE ADULT LARGE POLLO  (sólo 12KG)
    (15509680128387, 15509680030083, "12+2KG"),   # PREMIUM LINE ADULT TERNERA      (sólo 12KG)
    (15509676589443, 15509676654979, "12+2KG"),   # LIFE ADULT LARGE BREED POLLO    (sólo 12KG)
]

# Títulos limpios para los productos base que tenían el tamaño en el título
TITULO_LIMPIO_A = {
    15509679735171: "CALIBRA DOG PREMIUM LINE ADULT LARGE POLLO",
    15509680128387: "CALIBRA DOG PREMIUM LINE ADULT TERNERA",
    15509676589443: "CALIBRA DOG LIFE ADULT LARGE BREED POLLO",
}

GRUPO_B = [
    # (id_80g, id_250g, título_unificado)
    (15509682389379, 15509681635715, "CALIBRA JOY DOG CLASSIC STRIPS CORDERO"),
    (15509682454915, 15509681799555, "CALIBRA JOY DOG CLASSIC STRIPS PATO"),
    (15509682061699, 15509681963395, "CALIBRA JOY DOG CLASSIC SANDWICH PESCADO POLLO"),
    (15509682192771, 15509681996163, "CALIBRA JOY DOG CLASSIC STICKS SALMON"),
    (15509682291075, 15509682487683, "CALIBRA JOY DOG CLASSIC STICKS TERNERA"),
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
    """Copia todas las imágenes de src_pid a dst_pid, asignándolas a variant_ids si se indica."""
    images = get_images(api, src_pid)
    copied = []
    for img in images:
        payload = {"src": img["src"], "alt": img.get("alt") or ""}
        if variant_ids:
            payload["variant_ids"] = variant_ids
        r = _request("POST", f"{api.base}/products/{dst_pid}/images.json",
                     headers=api.h, json={"image": payload}, timeout=30)
        new_img = r.json().get("image", {})
        copied.append(new_img.get("id"))
        pause()
    return copied

def pause():
    time.sleep(0.5)   # evitar rate limit


# ---------------------------------------------------------------------------
# Grupo A
# ---------------------------------------------------------------------------

def procesar_grupo_a(api, base_id, bundle_id, bundle_label):
    print(f"\n[A] base={base_id} + bundle={bundle_id} → añadir variante '{bundle_label}'")

    base    = get_product(api, base_id)
    bundle  = get_product(api, bundle_id)
    pause()

    bundle_variant = bundle["variants"][0]
    base_options   = base["options"]

    # --- Caso 1: base ya tiene opción 'Peso' (múltiples variantes) ---
    if base_options[0]["name"] != "Title":
        option_name = base_options[0]["name"]   # "Peso"
        print(f"  opción existente: '{option_name}' — añadiendo variante directamente")
        new_variant = add_variant(api, base_id, {
            "option1":  bundle_label,
            "price":    bundle_variant["price"],
            "barcode":  bundle_variant["barcode"],
            "sku":      bundle_variant["sku"] or bundle_variant["barcode"],
            "weight":   bundle_variant.get("weight") or 0,
            "weight_unit": bundle_variant.get("weight_unit") or "kg",
        })
        print(f"  ✓ variante añadida: id={new_variant['id']} option1={new_variant['option1']}")

    # --- Caso 2: base tiene sólo Title/Default Title → convertir a Peso ---
    else:
        titulo_limpio = TITULO_LIMPIO_A.get(base_id, base["title"])
        existing_variant = base["variants"][0]
        print(f"  convirtiendo opción Title→Peso y título→'{titulo_limpio}'")

        # Actualizar producto: renombrar opción y limpiar título
        update_product(api, base_id, {
            "id":    base_id,
            "title": titulo_limpio,
            "options": [{"name": "Peso"}],
            "variants": [{
                "id":      existing_variant["id"],
                "option1": "12KG",
                "price":   existing_variant["price"],
                "barcode": existing_variant["barcode"],
                "sku":     existing_variant["sku"] or existing_variant["barcode"],
            }],
        })
        pause()
        print(f"  ✓ producto actualizado")

        # Añadir variante 12+2KG
        new_variant = add_variant(api, base_id, {
            "option1":  bundle_label,
            "price":    bundle_variant["price"],
            "barcode":  bundle_variant["barcode"],
            "sku":      bundle_variant["sku"] or bundle_variant["barcode"],
            "weight":   bundle_variant.get("weight") or 0,
            "weight_unit": bundle_variant.get("weight_unit") or "kg",
        })
        print(f"  ✓ variante añadida: id={new_variant['id']} option1={new_variant['option1']}")

    pause()

    # Copiar imágenes del bundle al producto base, asignadas a la nueva variante
    copied = copy_images(api, bundle_id, base_id, variant_ids=[new_variant["id"]])
    print(f"  ✓ {len(copied)} imagen(es) copiada(s) del bundle al producto base")

    # Eliminar producto bundle
    delete_product(api, bundle_id)
    print(f"  ✓ producto bundle {bundle_id} eliminado")


# ---------------------------------------------------------------------------
# Grupo B
# ---------------------------------------------------------------------------

def procesar_grupo_b(api, id_80g, id_250g, titulo_unificado):
    print(f"\n[B] {id_80g} (80g) + {id_250g} (250g) → '{titulo_unificado}'")

    p80  = get_product(api, id_80g)
    p250 = get_product(api, id_250g)
    pause()

    v80  = p80["variants"][0]
    v250 = p250["variants"][0]

    # Actualizar producto 80g: nuevo título, opción Peso, variante renombrada a 80GR
    update_product(api, id_80g, {
        "id":    id_80g,
        "title": titulo_unificado,
        "options": [{"name": "Peso"}],
        "variants": [{
            "id":      v80["id"],
            "option1": "80GR",
            "price":   v80["price"],
            "barcode": v80["barcode"],
            "sku":     v80["sku"] or v80["barcode"],
        }],
    })
    pause()
    print(f"  ✓ producto actualizado: título='{titulo_unificado}', variante 80GR")

    # Añadir variante 250GR
    new_variant = add_variant(api, id_80g, {
        "option1":  "250GR",
        "price":    v250["price"],
        "barcode":  v250["barcode"],
        "sku":      v250["sku"] or v250["barcode"],
        "weight":   v250.get("weight") or 0,
        "weight_unit": v250.get("weight_unit") or "g",
    })
    print(f"  ✓ variante añadida: id={new_variant['id']} option1=250GR EAN={new_variant['barcode']}")
    pause()

    # Guardar IDs de imágenes existentes del 80g (antes de copiar las del 250g)
    imgs_base = get_images(api, id_80g)

    # Asignar imagen principal del 80g a su variante
    if imgs_base:
        img_80g = imgs_base[0]
        _request("PUT", f"{api.base}/variants/{v80['id']}.json",
                 headers=api.h,
                 json={"variant": {"id": v80["id"], "image_id": img_80g["id"]}},
                 timeout=30)
        pause()
        print(f"  ✓ imagen base asignada a variante 80GR (img_id={img_80g['id']})")

    # Copiar imágenes del 250g al producto unificado, asignadas a la variante 250GR
    copied = copy_images(api, id_250g, id_80g, variant_ids=[new_variant["id"]])
    print(f"  ✓ {len(copied)} imagen(es) copiada(s) del 250g al producto unificado")

    # Eliminar producto 250g
    delete_product(api, id_250g)
    print(f"  ✓ producto 250g {id_250g} eliminado")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("=== DRY RUN — no se realizarán cambios en Shopify ===\n")
        print("Grupo A:")
        for base_id, bundle_id, label in GRUPO_A:
            print(f"  base={base_id} + bundle={bundle_id} → añadir '{label}'")
        print("\nGrupo B:")
        for id_80g, id_250g, titulo in GRUPO_B:
            print(f"  {id_80g} (80g) + {id_250g} (250g) → '{titulo}'")
        return

    token = get_token()
    api   = ShopifyAPI(token)

    print("=" * 60)
    print("GRUPO A — Bundles → variante del producto base")
    print("=" * 60)
    for base_id, bundle_id, label in GRUPO_A:
        try:
            procesar_grupo_a(api, base_id, bundle_id, label)
        except Exception as e:
            print(f"  ✗ ERROR en A ({base_id}/{bundle_id}): {e}")

    print("\n" + "=" * 60)
    print("GRUPO B — 80g + 250g → producto unificado")
    print("=" * 60)
    for id_80g, id_250g, titulo in GRUPO_B:
        try:
            procesar_grupo_b(api, id_80g, id_250g, titulo)
        except Exception as e:
            print(f"  ✗ ERROR en B ({id_80g}/{id_250g}): {e}")

    print("\n✓ Proceso completado")


if __name__ == "__main__":
    main()
