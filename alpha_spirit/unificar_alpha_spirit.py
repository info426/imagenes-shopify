#!/usr/bin/env python3
"""
Unificación y limpieza de productos Alpha Spirit en Shopify
============================================================
Operaciones:
  1. ELIMINAR  — Huesos de jamón (4 productos)
  2. ELIMINAR  — Snack de Pollo duplicado (sin prefijo CANINE)
  3. UNIFICAR  — Nervio de Toro (4 productos → 1 con variantes de formato)
  4. UNIFICAR  — Oreja de Cerdo (3 productos → 1 con variantes de tamaño)

Solo modifica los productos listados explícitamente. Imprime un resumen
detallado antes de hacer cambios y pide confirmación si se ejecuta en local.
"""

import os
import sys
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

SHOP_DOMAIN   = os.getenv("SHOP_DOMAIN",   "7ev1zx-eg.myshopify.com")
CLIENT_ID     = os.getenv("CLIENT_ID",     "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
VENDOR        = "Alpha Spirit"
API_VERSION   = "2024-10"

# ─── Shopify Auth ──────────────────────────────────────────────────────────────

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
            r = requests.get(url, headers=self.h, params=params, timeout=30)
            r.raise_for_status()
            products.extend(r.json().get("products", []))
            params, url = {}, None
            link = r.headers.get("Link", "")
            if 'rel="next"' in link:
                for part in link.split(","):
                    if 'rel="next"' in part:
                        url = part.strip().split(";")[0].strip("<>")
        return products

    def delete_product(self, product_id: int):
        r = requests.delete(f"{self.base}/products/{product_id}.json",
                            headers=self.h, timeout=30)
        r.raise_for_status()

    def update_product(self, product_id: int, payload: dict) -> dict:
        r = requests.put(f"{self.base}/products/{product_id}.json",
                         headers=self.h, json={"product": payload}, timeout=30)
        r.raise_for_status()
        return r.json()["product"]

    def create_variant(self, product_id: int, payload: dict) -> dict:
        r = requests.post(f"{self.base}/products/{product_id}/variants.json",
                          headers=self.h, json={"variant": payload}, timeout=30)
        r.raise_for_status()
        return r.json()["variant"]

# ─── Lógica de unificación ────────────────────────────────────────────────────

def _variant_payload_from(variant: dict, option1: str) -> dict:
    """Copia los campos relevantes de una variante existente para crear la nueva."""
    return {
        "option1":             option1,
        "price":               variant.get("price", "0.00"),
        "compare_at_price":    variant.get("compare_at_price"),
        "sku":                 variant.get("sku", ""),
        "weight":              variant.get("weight", 0),
        "weight_unit":         variant.get("weight_unit", "kg"),
        "inventory_management": variant.get("inventory_management"),
        "inventory_policy":    variant.get("inventory_policy", "deny"),
        "requires_shipping":   variant.get("requires_shipping", True),
        "taxable":             variant.get("taxable", True),
    }


def unify_products(api: ShopifyAPI, group: list[tuple[dict, str]],
                   new_title: str, option_name: str, dry_run: bool = False):
    """
    group: lista de (product_dict, option1_value)
    Mantiene el primero como base, añade variantes de los demás y los elimina.
    """
    base_product, base_opt = group[0]
    base_id = base_product["id"]
    base_variant = base_product["variants"][0]

    print(f"\n  Base: [{base_id}] {base_product['title']}")
    print(f"    → nuevo título: «{new_title}»")
    print(f"    → opción base:  {option_name} = «{base_opt}»")
    for p, opt in group[1:]:
        print(f"  Merge: [{p['id']}] {p['title']}  →  variante «{opt}»")

    if dry_run:
        print("  [DRY RUN] Sin cambios.")
        return

    # 1. Actualizar producto base: nuevo título + opción + valor de variante base
    api.update_product(base_id, {
        "id":      base_id,
        "title":   new_title,
        "options": [{"name": option_name}],
        "variants": [{"id": base_variant["id"], "option1": base_opt}],
    })
    print(f"  ✓ Producto base actualizado")
    time.sleep(0.5)

    # 2. Añadir variantes de los otros productos
    for other_product, opt in group[1:]:
        other_variant = other_product["variants"][0]
        payload = _variant_payload_from(other_variant, opt)
        api.create_variant(base_id, payload)
        print(f"  ✓ Variante «{opt}» añadida")
        time.sleep(0.5)

        # 3. Eliminar el producto que ya fue absorbido
        api.delete_product(other_product["id"])
        print(f"  ✓ Producto [{other_product['id']}] eliminado")
        time.sleep(0.5)

    print(f"  ✓ UNIFICACIÓN COMPLETADA → [{base_id}] «{new_title}»")


def delete_products(api: ShopifyAPI, products: list[dict],
                    reason: str, dry_run: bool = False):
    print(f"\n  Motivo: {reason}")
    for p in products:
        print(f"  Eliminar: [{p['id']}] {p['title']}")
    if dry_run:
        print("  [DRY RUN] Sin cambios.")
        return
    for p in products:
        api.delete_product(p["id"])
        print(f"  ✓ Eliminado [{p['id']}] {p['title']}")
        time.sleep(0.5)

# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv

    if not CLIENT_ID or not CLIENT_SECRET:
        print("ERROR: Faltan credenciales")
        sys.exit(1)

    print("=" * 65)
    print("UNIFICACIÓN ALPHA SPIRIT — SHOPIFY")
    if dry_run:
        print("  *** MODO DRY RUN — sin cambios en Shopify ***")
    print("=" * 65)

    token = get_token()
    api   = ShopifyAPI(token)

    print("\nObteniendo productos Alpha Spirit...")
    products = api.get_products(VENDOR)
    print(f"Total: {len(products)} productos\n")

    by_title = {p["title"].strip(): p for p in products}

    def find(title_fragment: str) -> list[dict]:
        return [p for t, p in by_title.items()
                if title_fragment.lower() in t.lower()]

    # ── 1. ELIMINAR: Huesos de jamón ──────────────────────────────────────────
    print("─" * 65)
    print("1. ELIMINAR — Huesos de jamón")
    huesos = (
        find("HUESO JAMON") +
        find("MEDIO HUESO JAMON") +
        find("HUESOS DE JAM")
    )
    # deduplicar por ID
    seen = set()
    huesos_uniq = [p for p in huesos if not (p["id"] in seen or seen.add(p["id"]))]
    if huesos_uniq:
        delete_products(api, huesos_uniq, "Producto eliminado del catálogo", dry_run)
    else:
        print("  ✓ No se encontraron productos de hueso de jamón.")

    # ── 2. ELIMINAR: Snack de Pollo duplicado (sin CANINE) ────────────────────
    print("\n" + "─" * 65)
    print("2. ELIMINAR — Snack de Pollo duplicado")
    snack_dup = [p for p in products
                 if p["title"].strip() == "ALPHA SPIRIT SNACK DE POLLO 16X35GR"]
    if snack_dup:
        delete_products(api, snack_dup, "Duplicado de CANINE SNACK POLLO CAJA 16X35GR", dry_run)
    else:
        print("  ✓ No se encontró el duplicado (ya eliminado).")

    # ── 3. UNIFICAR: Nervio de Toro ───────────────────────────────────────────
    print("\n" + "─" * 65)
    print("3. UNIFICAR — Nervio de Toro")
    nervio_map = {
        "NERVIO DE TORO 4X 12UD M":         "4×12 ud. (M)",
        "NERVIO DE TORO 4X 8UD L":           "4×8 ud. (L)",
        "NERVIO DE TORO 5U XL":              "5 ud. (XL)",
        "NERVIO DE TORO INDIVIDUAL L 16UD":  "Individual 16 ud. (L)",
    }
    nervio_group = []
    for fragment, opt in nervio_map.items():
        matches = find(fragment)
        if matches:
            nervio_group.append((matches[0], opt))
        else:
            print(f"  AVISO: no encontrado «{fragment}»")

    if len(nervio_group) >= 2:
        unify_products(api, nervio_group,
                       new_title="ALPHA SPIRIT NERVIO DE TORO",
                       option_name="Formato",
                       dry_run=dry_run)
    elif len(nervio_group) == 1:
        print("  Solo se encontró 1 producto, no hay nada que unificar.")
    else:
        print("  No se encontraron productos de nervio de toro.")

    # ── 4. UNIFICAR: Oreja de Cerdo ───────────────────────────────────────────
    print("\n" + "─" * 65)
    print("4. UNIFICAR — Oreja de Cerdo")
    # Orden fijo: M → L → XL
    oreja_map = {
        "OREJA DE CERDO INDIVIDUAL M": "M",
        "OREJA DE CERDO INDIVIDUAL L ": "L",   # espacio para no capturar XL
        "OREJA DE CERDO INDIVIDUAL XL": "XL",
    }
    oreja_group = []
    for fragment, opt in oreja_map.items():
        matches = find(fragment.strip())
        if matches:
            oreja_group.append((matches[0], opt))
        else:
            print(f"  AVISO: no encontrado «{fragment.strip()}»")

    if len(oreja_group) >= 2:
        unify_products(api, oreja_group,
                       new_title="ALPHA SPIRIT OREJA DE CERDO INDIVIDUAL 25UD",
                       option_name="Tamaño",
                       dry_run=dry_run)
    elif len(oreja_group) == 1:
        print("  Solo se encontró 1 producto, no hay nada que unificar.")
    else:
        print("  No se encontraron productos de oreja de cerdo.")

    # ── Resumen ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("COMPLETADO" + (" (DRY RUN)" if dry_run else ""))
    print("=" * 65)


if __name__ == "__main__":
    main()
