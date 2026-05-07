#!/usr/bin/env python3
"""
Análisis de productos Farmina ND en Shopify
=============================================
Solo lectura — no modifica nada en la tienda.
Muestra todos los vendors que contienen "farmina" y el estado
de imágenes de los productos Farmina ND.
"""

import os
import sys
import requests
from io import BytesIO
from PIL import Image
from dotenv import load_dotenv

load_dotenv()

SHOP_DOMAIN   = os.getenv("SHOP_DOMAIN",   "7ev1zx-eg.myshopify.com")
CLIENT_ID     = os.getenv("CLIENT_ID",     "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
API_VERSION   = "2024-10"
TARGET_SIZE   = (2000, 2000)

# ─── Token ───────────────────────────────────────────────────────────────────

def get_token() -> str:
    resp = requests.post(
        f"https://{SHOP_DOMAIN}/admin/oauth/access_token",
        data={
            "grant_type":    "client_credentials",
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        print(f"ERROR al obtener token: {resp.text}")
        sys.exit(1)
    return token

# ─── API ─────────────────────────────────────────────────────────────────────

def get_all_products(token: str) -> list:
    headers = {"X-Shopify-Access-Token": token}
    products, params = [], {"limit": 250, "fields": "id,title,vendor,images"}
    url = f"https://{SHOP_DOMAIN}/admin/api/{API_VERSION}/products.json"
    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        products.extend(resp.json().get("products", []))
        params = {}
        url = None
        link = resp.headers.get("Link", "")
        if 'rel="next"' in link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.strip().split(";")[0].strip("<>")
    return products

def get_image_info(url: str) -> dict:
    """Descarga la imagen y devuelve sus características."""
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content))
        size_kb = len(resp.content) // 1024

        return {
            "size":   img.size,
            "mode":   img.mode,
            "format": img.format or "?",
            "kb":     size_kb,
            "ok":     img.size == TARGET_SIZE and img.format == "JPEG",
        }
    except Exception as exc:
        return {"error": str(exc)}

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("ANÁLISIS FARMINA ND — SHOPIFY (solo lectura)")
    print("=" * 65)

    token    = get_token()
    products = get_all_products(token)
    print(f"\nTotal productos en la tienda: {len(products)}\n")

    # ── 1. Vendors únicos que contienen "farmina" ─────────────────────────
    vendors = sorted({p["vendor"] for p in products
                      if "farmina" in p["vendor"].lower()})
    print("Vendors con 'farmina' encontrados:")
    for v in vendors:
        count = sum(1 for p in products if p["vendor"] == v)
        print(f"  · {v!r:<30} ({count} productos)")

    # ── 2. Filtrar Farmina ND (excluir Vet Life) ──────────────────────────
    farmina_nd = [
        p for p in products
        if "farmina" in p["vendor"].lower()
        and "vet life" not in p["vendor"].lower()
        and "vet life" not in p["title"].lower()
    ]
    print(f"\nProductos Farmina ND (sin Vet Life): {len(farmina_nd)}")

    # ── 3. Análisis imagen por imagen ─────────────────────────────────────
    necesitan_trabajo = []
    sin_imagen        = []

    print(f"\n{'#':<4} {'PRODUCTO':<45} {'IMGS':<5} {'ESTADO'}")
    print("-" * 75)

    for i, p in enumerate(farmina_nd, 1):
        pid    = p["id"]
        title  = p["title"][:44]
        images = p.get("images", [])

        if not images:
            sin_imagen.append(p)
            print(f"{i:<4} {title:<45} {'0':<5} ⚠ SIN IMAGEN")
            continue

        problemas = []
        for img_data in images:
            info = get_image_info(img_data["src"])
            if "error" in info:
                problemas.append(f"Error: {info['error']}")
            elif not info["ok"]:
                detalles = []
                if info["size"] != TARGET_SIZE:
                    detalles.append(f"{info['size'][0]}×{info['size'][1]}")
                if info["format"] != "JPEG":
                    detalles.append(info["format"])
                problemas.append(", ".join(detalles) if detalles else "requiere reoptimización")

        n = len(images)
        if problemas:
            necesitan_trabajo.append({"product": p, "issues": problemas})
            resumen = " | ".join(problemas)
            print(f"{i:<4} {title:<45} {n:<5} ✗ {resumen}")
        else:
            print(f"{i:<4} {title:<45} {n:<5} ✓ OK")

    # ── 4. Resumen ────────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("RESUMEN")
    print("=" * 65)
    print(f"  Total Farmina ND          : {len(farmina_nd)}")
    print(f"  Necesitan procesado       : {len(necesitan_trabajo)}")
    print(f"  Sin imagen (buscar en web): {len(sin_imagen)}")
    ok = len(farmina_nd) - len(necesitan_trabajo) - len(sin_imagen)
    print(f"  Ya cumplen requisitos     : {ok}")

    if necesitan_trabajo:
        print("\nProductos que necesitan trabajo:")
        for item in necesitan_trabajo:
            p = item["product"]
            print(f"  ID {p['id']} · {p['title']}")
            for iss in item["issues"]:
                print(f"           → {iss}")

    if sin_imagen:
        print("\nProductos sin imagen:")
        for p in sin_imagen:
            print(f"  ID {p['id']} · {p['title']}")

    print("\nAnálisis completado. Pega este resultado para continuar.")


if __name__ == "__main__":
    main()
