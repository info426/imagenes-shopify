#!/usr/bin/env python3
"""
Diagnóstico de la estructura de farmina.com para Vet Life
Solo lectura — ayuda a entender cómo scraping las imágenes.
"""

import requests
import re
import json
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        print(f"  GET {url} → {r.status_code} ({len(r.content)} bytes)")
        return r
    except Exception as e:
        print(f"  GET {url} → ERROR: {e}")
        return None

def find_images_in_html(html, label=""):
    soup = BeautifulSoup(html, "html.parser")
    imgs = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and any(ext in src.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"]):
            imgs.append(src)
    if imgs:
        print(f"  [{label}] {len(imgs)} imágenes encontradas:")
        for i in imgs[:5]:
            print(f"    {i}")
    return imgs

def find_product_links(html, base_url="https://www.farmina.com"):
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if any(kw in href.lower() or kw in text.lower()
               for kw in ["vet", "cardiac", "renal", "hepatic", "obesity"]):
            full = href if href.startswith("http") else base_url + href
            links.append((text[:60], full))
    return links[:20]

print("=" * 60)
print("DIAGNÓSTICO FARMINA.COM — ESTRUCTURA VET LIFE")
print("=" * 60)

# 1. Página principal de Vet Life en diferentes URLs
test_urls = [
    "https://www.farmina.com/es/vet-life/",
    "https://www.farmina.com/vet-life/",
    "https://www.farmina.com/es/producto/vet-life-dog-cardiac/",
    "https://www.farmina.com/es/?s=vet+life+cardiac",
    "https://www.farmina.com/?s=vet+life+cardiac",
    "https://www.farmina.com/es/productos/?linea=vet-life",
]

for url in test_urls:
    r = fetch(url)
    if r and r.status_code == 200:
        print(f"  ✓ Accesible")
        links = find_product_links(r.text)
        if links:
            print(f"  Links relevantes encontrados:")
            for text, href in links[:10]:
                print(f"    [{text}] → {href}")
        find_images_in_html(r.text, url.split("/")[-2])
        # Guardar HTML para inspección
        fname = url.replace("https://", "").replace("/", "_").strip("_") + ".html"
        with open(f"/tmp/{fname}", "w", encoding="utf-8") as f:
            f.write(r.text)
        print(f"  HTML guardado en /tmp/{fname}")
    print()

# 2. Intentar sitemap
print("\n--- SITEMAP ---")
for sitemap in ["https://www.farmina.com/sitemap.xml",
                "https://www.farmina.com/es/sitemap.xml"]:
    r = fetch(sitemap)
    if r and r.status_code == 200:
        urls_vet = re.findall(r'<loc>(.*?vet[^<]*)</loc>', r.text, re.IGNORECASE)
        print(f"  URLs con 'vet' en sitemap: {len(urls_vet)}")
        for u in urls_vet[:10]:
            print(f"    {u}")

print("\nDiagnóstico completado.")
