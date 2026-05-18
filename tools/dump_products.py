"""Descarga todos los productos de un vendor y los vuelca a JSON con variantes y EAN."""

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.shopify_api import ShopifyAPI, get_token

def main():
    vendor = sys.argv[1] if len(sys.argv) > 1 else "CALIBRA"
    out_path = sys.argv[2] if len(sys.argv) > 2 else f"resultados/{vendor.lower()}_productos.json"

    token = get_token()
    api = ShopifyAPI(token)

    print(f"Descargando productos vendor={vendor}...", flush=True)
    products = api.get_products(vendor)
    print(f"  {len(products)} productos encontrados", flush=True)

    result = []
    for p in products:
        variants = []
        for v in p.get("variants", []):
            variants.append({
                "id": v["id"],
                "title": v["title"],
                "sku": v.get("sku", ""),
                "barcode": v.get("barcode", ""),
                "weight": v.get("weight"),
                "weight_unit": v.get("weight_unit"),
                "price": v.get("price"),
                "inventory_quantity": v.get("inventory_quantity"),
                "option1": v.get("option1"),
                "option2": v.get("option2"),
                "option3": v.get("option3"),
            })
        result.append({
            "id": p["id"],
            "title": p["title"],
            "handle": p["handle"],
            "vendor": p.get("vendor", ""),
            "product_type": p.get("product_type", ""),
            "tags": p.get("tags", ""),
            "status": p.get("status", ""),
            "options": [o["name"] for o in p.get("options", [])],
            "variants": variants,
        })

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"Guardado en {out_path}", flush=True)

if __name__ == "__main__":
    main()
