#!/usr/bin/env python3
"""
Diagnóstico fase 4: URL canónica eshop + cookies + producto individual
"""

import re
import json
import requests
from bs4 import BeautifulSoup

BASE = "https://www.farmina.com"

# Aceptar cookies para desbloquear contenido
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.farmina.com/es/",
})
# Cookies típicas de consentimiento
SESSION.cookies.update({
    "cookieConsent": "1",
    "cookie_consent": "accepted",
    "has_js": "1",
})

def fetch(url):
    try:
        r = SESSION.get(url, timeout=20)
        print(f"  GET {url[:90]} → {r.status_code} ({len(r.content)} bytes)")
        return r if r.status_code == 200 else None
    except Exception as e:
        print(f"  ERROR: {e}")
        return None

def inspect_products(html, label=""):
    soup = BeautifulSoup(html, "html.parser")

    # Todos los links que podrían ser productos
    product_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        # Buscar links que parezcan productos (tienen número en URL o keyword)
        if re.search(r'/\d+-\w', href) or any(
            kw in href.lower() for kw in ["cardiac", "renal", "hepatic",
                                           "obesity", "struvite", "diabetic",
                                           "hypo", "joint", "vet-life"]):
            product_links.append((text[:60], href))

    if product_links:
        print(f"  [{label}] Links de producto encontrados: {len(product_links)}")
        for t, h in product_links[:20]:
            print(f"    [{t}] → {h}")
    else:
        print(f"  [{label}] Sin links de producto")

    # Mostrar más HTML (central)
    mid = len(html) // 2
    print(f"\n  HTML central (chars {mid}-{mid+1000}):")
    print(html[mid:mid+1000])

    return product_links

print("=" * 65)
print("DIAGNÓSTICO FASE 4 — URL CANÓNICA + COOKIES + PRODUCTO INDIVIDUAL")
print("=" * 65)

# 1. Probar URLs canónicas con /eshop-dog/ y /eshop-cat/
test_urls = [
    f"{BASE}/es/eshop-dog/alimento-para-perros/8-farmina-vet-life.html",
    f"{BASE}/es/eshop-cat/alimento-para-gatos/14-farmina-vet-life.html",
    f"{BASE}/es/eshop-dog/",
    f"{BASE}/es/eshop-cat/",
]

all_links = []
for url in test_urls:
    print(f"\n--- {url} ---")
    r = fetch(url)
    if r:
        links = inspect_products(r.text, url.split("/")[-1])
        all_links.extend(links)

# 2. Buscar imagen de producto individual si encontramos links
if all_links:
    print(f"\n--- PRUEBA IMAGEN PRIMER PRODUCTO ---")
    name, url = all_links[0]
    full_url = url if url.startswith("http") else BASE + url
    print(f"  Producto: {name}")
    r = fetch(full_url)
    if r:
        soup = BeautifulSoup(r.text, "html.parser")
        print(f"  Título: {soup.title.text.strip() if soup.title else 'N/A'}")

        # Buscar imágenes grandes
        imgs = []
        for img in soup.find_all("img"):
            src = (img.get("data-zoom-image") or img.get("data-src") or
                   img.get("src") or "")
            if src and any(ext in src for ext in [".jpg", ".jpeg", ".png"]):
                w = int(img.get("width", 0) or 0)
                imgs.append((w, src))
        imgs.sort(reverse=True)
        print(f"  Imágenes encontradas: {len(imgs)}")
        for w, s in imgs[:5]:
            print(f"    {w}px: {s}")

        # JSON con datos de producto
        for script in soup.find_all("script"):
            text = script.string or ""
            if "image" in text.lower() and len(text) > 100:
                urls = re.findall(r'https?://[^\s"\']+\.(?:jpg|jpeg|png)', text)
                if urls:
                    print(f"  URLs en scripts: {urls[:5]}")
                    break

# 3. Probar URLs directas de productos típicos
print(f"\n--- PRUEBA URLs DIRECTAS DE PRODUCTOS ---")
direct_attempts = [
    f"{BASE}/es/eshop-dog/alimento-para-perros/vet-life-natural-canine-cardiac.html",
    f"{BASE}/es/eshop-dog/alimento-para-perros/vet-life-dog-cardiac.html",
    f"{BASE}/es/vet-life-dog-cardiac.html",
    f"{BASE}/es/alimento-para-perros/vet-life-dog-cardiac.html",
]
for url in direct_attempts:
    r = fetch(url)
    if r:
        soup = BeautifulSoup(r.text, "html.parser")
        print(f"  Título: {soup.title.text.strip() if soup.title else 'N/A'}")

# 4. Imagen actual de Shopify como referencia de nombre de archivo
print(f"\n--- IMAGEN SHOPIFY COMO REFERENCIA ---")
# La imagen actual en Shopify para DOG CARDIAC la obtenemos de la API
import os
shop_domain = "7ev1zx-eg.myshopify.com"
client_id = "351cda3bbb4fd14fbda696b30792ca25"
client_secret = "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"
token_resp = requests.post(
    f"https://{shop_domain}/admin/oauth/access_token",
    data={"grant_type": "client_credentials",
          "client_id": client_id, "client_secret": client_secret},
    timeout=15
)
if token_resp.status_code == 200:
    token = token_resp.json().get("access_token")
    img_resp = requests.get(
        f"https://{shop_domain}/admin/api/2024-10/products/15509747827075/images.json",
        headers={"X-Shopify-Access-Token": token}, timeout=15
    )
    if img_resp.status_code == 200:
        images = img_resp.json().get("images", [])
        for img in images:
            print(f"  URL actual Shopify: {img['src']}")
            # Extraer nombre de archivo
            fname = img['src'].split("?")[0].split("/")[-1]
            print(f"  Nombre de archivo: {fname}")
            # Intentar encontrar ese nombre en farmina.com
            farmina_attempts = [
                f"{BASE}/img/p/{fname}",
                f"{BASE}/images/{fname}",
                f"{BASE}/media/{fname}",
            ]
            for fa in farmina_attempts:
                r = fetch(fa)
                if r:
                    print(f"  ✓ ENCONTRADO: {fa}")

print("\nDiagnóstico completado.")
