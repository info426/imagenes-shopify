#!/usr/bin/env python3
"""
Análisis de productos Farmina Vet Life en Shopify
===================================================
Solo lectura — no modifica nada en la tienda.
Muestra el estado actual de imágenes de los 59 productos Vet Life.
"""

import os
import sys
import requests
from io import BytesIO
from PIL import Image
from dotenv import load_dotenv

load_dotenv()

SHOP_DOMAIN   = os.getenv("SHOP_DOMAIN", "7ev1zx-eg.myshopify.com")
CLIENT_ID     = os.getenv("CLIENT_ID",   "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
API_VERSION   = "2024-10"
TARGET_SIZE   = (2000, 2000)
VENDOR        = "Farmina Vet Life"

# ─── Token ───────────────────────────────────────────────────────────────────

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
        print(f"ERROR al obtener token: {resp.text}")
        sys.exit(1)
    return token

# ─── API ─────────────────────────────────────────────────────────────────────

def get_products(token: str, vendor: str) -> list:
    headers = {"X-Shopify-Access-Token": token}
    products, params = [], {"limit": 250, "vendor": vendor,
                            "fields": "id,title,vendor,images"}
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
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content))
        url_clean = url.split("?")[0].lower()
        url_is_jpg = url_clean.endswith(".jpg") or url_clean.endswith(".jpeg")
        content_is_jpeg = img.format == "JPEG"
        ok = img.size == TARGET_SIZE and content_is_jpeg and url_is_jpg
        return {"size": img.size, "format": img.format or "?",
                "kb": len(resp.content) // 1024, "ok": ok,
                "url_ext": "jpg" if url_is_jpg else "png"}
    except Exception as exc:
        return {"error": str(exc)}

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("ANÁLISIS FARMINA VET LIFE — SHOPIFY (solo lectura)")
    print("=" * 65)

    token    = get_token()
    products = get_products(token, VENDOR)
    print(f"\nProductos Farmina Vet Life encontrados: {len(products)}\n")

    necesitan_trabajo, sin_imagen = [], []

    print(f"\n{'#':<4} {'PRODUCTO':<48} {'IMGS':<5} {'ESTADO'}")
    print("-" * 78)

    for i, p in enumerate(products, 1):
        pid    = p["id"]
        title  = p["title"][:47]
        images = p.get("images", [])

        if not images:
            sin_imagen.append(p)
            print(f"{i:<4} {title:<48} {'0':<5} ⚠ SIN IMAGEN")
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
                if info["format"] != "JPEG" or info["url_ext"] != "jpg":
                    detalles.append(f"{info['format']}(.{info['url_ext']})")
                problemas.append(", ".join(detalles) if detalles else "pendiente")

        n = len(images)
        if problemas:
            necesitan_trabajo.append({"product": p, "issues": problemas,
                                      "n_images": n})
            resumen = " | ".join(problemas)
            print(f"{i:<4} {title:<48} {n:<5} ✗ {resumen}")
        else:
            print(f"{i:<4} {title:<48} {n:<5} ✓ OK")

    print("\n" + "=" * 65)
    print("RESUMEN")
    print("=" * 65)
    print(f"  Total Farmina Vet Life    : {len(products)}")
    print(f"  Necesitan procesado       : {len(necesitan_trabajo)}")
    print(f"  Sin imagen                : {len(sin_imagen)}")
    ok = len(products) - len(necesitan_trabajo) - len(sin_imagen)
    print(f"  Ya cumplen requisitos     : {ok}")

    print("\nDetalle productos a procesar:")
    for item in necesitan_trabajo:
        p = item["product"]
        print(f"  ID {p['id']} · {p['title']} ({item['n_images']} imgs)")

    if sin_imagen:
        print("\nSin imagen:")
        for p in sin_imagen:
            print(f"  ID {p['id']} · {p['title']}")

    print("\nAnálisis completado.")

if __name__ == "__main__":
    main()
