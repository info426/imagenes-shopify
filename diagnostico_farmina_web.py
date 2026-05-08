#!/usr/bin/env python3
"""
Diagnóstico fase 2: explorar categorías Vet Life en farmina.com (PrestaShop)
"""

import re
import json
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

BASE = "https://www.farmina.com"

CAT_URLS = [
    f"{BASE}/es/alimento-para-perros/8-farmina-vet-life.html",
    f"{BASE}/es/alimento-para-gatos/14-farmina-vet-life.html",
]

def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        print(f"  GET {url} → {r.status_code} ({len(r.content)} bytes)")
        return r if r.status_code == 200 else None
    except Exception as e:
        print(f"  ERROR: {e}")
        return None

def scrape_category(url):
    """Extrae todos los productos de una página de categoría PrestaShop."""
    products = []
    page = 1
    while True:
        page_url = url if page == 1 else f"{url}?page={page}"
        r = fetch(page_url)
        if not r:
            break
        soup = BeautifulSoup(r.text, "html.parser")

        # PrestaShop: productos en .product-miniature o article.product-miniature
        items = soup.select("article.product-miniature, .product-miniature, .js-product")
        if not items:
            # Intentar con otros selectores
            items = soup.select(".product_list .product-container, li.product-type-simple")

        if not items:
            print(f"  No se encontraron productos en página {page}")
            # Guardar HTML para inspección
            with open(f"/tmp/cat_page{page}.html", "w", encoding="utf-8") as f:
                f.write(r.text)
            print(f"  HTML guardado en /tmp/cat_page{page}.html")
            break

        print(f"  Página {page}: {len(items)} productos")
        for item in items:
            a = item.select_one("a.product-thumbnail, h3 a, .product-name a, a")
            img = item.select_one("img")
            name = ""
            href = ""
            img_src = ""
            if a:
                name = a.get_text(strip=True) or a.get("title", "")
                href = a.get("href", "")
            if img:
                img_src = (img.get("data-src") or img.get("src") or "")
            products.append({"name": name, "url": href, "thumb": img_src})

        # Verificar si hay página siguiente
        next_btn = soup.select_one("a[rel='next'], .next a, li.next a")
        if not next_btn:
            break
        page += 1

    return products

def get_product_image(product_url):
    """Obtiene la imagen de mayor resolución de una página de producto PrestaShop."""
    r = fetch(product_url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    # PrestaShop: imagen principal en .product-cover, #product-images-large
    candidates = []

    # Buscar en JSON de producto (más fiable)
    for script in soup.find_all("script"):
        text = script.string or ""
        # PrestaShop guarda datos de producto en JSON
        m = re.search(r'"large":\s*\{[^}]*"url"\s*:\s*"([^"]+)"', text)
        if m:
            candidates.append(m.group(1).replace("\\/", "/"))
        # Buscar array de imágenes
        urls = re.findall(r'"url_zoom"\s*:\s*"([^"]+)"', text)
        candidates.extend(u.replace("\\/", "/") for u in urls)
        urls2 = re.findall(r'"large_image_url"\s*:\s*"([^"]+)"', text)
        candidates.extend(u.replace("\\/", "/") for u in urls2)

    # Selectores CSS de producto principal
    for sel in [
        "img.zoomImg", ".product-cover img",
        "#bigpic", ".cloudzoom",
        ".product-image-main img",
        "img[itemprop='image']",
    ]:
        el = soup.select_one(sel)
        if el:
            src = el.get("data-zoom-image") or el.get("data-src") or el.get("src") or ""
            if src:
                candidates.append(src)

    # Devolver primera URL válida de imagen grande
    for c in candidates:
        if c and any(ext in c.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"]):
            return c if c.startswith("http") else BASE + c

    return None

# ─── MAIN ─────────────────────────────────────────────────────────────────────

print("=" * 65)
print("DIAGNÓSTICO FASE 2 — CATEGORÍAS VET LIFE EN FARMINA.COM")
print("=" * 65)

all_products = []

for cat_url in CAT_URLS:
    print(f"\n{'─'*65}")
    print(f"Categoría: {cat_url}")
    products = scrape_category(cat_url)
    print(f"Total productos en categoría: {len(products)}")
    for p in products:
        print(f"  · {p['name'][:60]}")
        print(f"    URL: {p['url']}")
    all_products.extend(products)

# Guardar mapa completo
with open("/tmp/farmina_vet_life_products.json", "w", encoding="utf-8") as f:
    json.dump(all_products, f, ensure_ascii=False, indent=2)
print(f"\n\nTotal productos encontrados: {len(all_products)}")
print("Mapa guardado en /tmp/farmina_vet_life_products.json")

# Probar obtener imagen de un producto de prueba
if all_products:
    print("\n--- PRUEBA: obtener imagen de primer producto ---")
    test = all_products[0]
    print(f"Producto: {test['name']}")
    print(f"URL: {test['url']}")
    img = get_product_image(test['url'])
    print(f"Imagen encontrada: {img}")

print("\nDiagnóstico completado.")
