#!/usr/bin/env python3
"""
Diagnóstico fase 3: encontrar endpoints AJAX / API de farmina.com PrestaShop
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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

BASE = "https://www.farmina.com"

def fetch(url, extra_headers=None):
    h = {**HEADERS, **(extra_headers or {})}
    try:
        r = requests.get(url, headers=h, timeout=20)
        print(f"  GET {url[:80]} → {r.status_code} ({len(r.content)} bytes)")
        return r if r.status_code == 200 else None
    except Exception as e:
        print(f"  ERROR: {e}")
        return None

def inspect_html(html, label=""):
    soup = BeautifulSoup(html, "html.parser")

    # Título
    title = soup.find("title")
    print(f"  Título: {title.text.strip() if title else 'N/A'}")

    # Buscar scripts con URLs de API / JSON con productos
    api_patterns = [
        r'(https?://[^\s"\']+(?:api|ajax|json|products?|catalog)[^\s"\']*)',
        r'"url"\s*:\s*"(https?://[^"]+)"',
        r"prestashop\s*=\s*(\{.*?\});",
        r'var\s+\w+\s*=\s*(\{[^;]{50,}\});',
    ]
    found_apis = set()
    found_json = []
    for script in soup.find_all("script"):
        text = script.string or ""
        for pat in api_patterns[:2]:
            for m in re.findall(pat, text, re.IGNORECASE):
                found_apis.add(m)
        # Buscar objeto prestashop
        m = re.search(r'prestashop\s*=\s*(\{.{20,5000}?\});', text, re.DOTALL)
        if m:
            found_json.append(('prestashop', m.group(1)[:500]))
        # Buscar arrays de productos
        m2 = re.search(r'"products"\s*:\s*(\[.{10,}\])', text, re.DOTALL)
        if m2:
            found_json.append(('products_array', m2.group(1)[:500]))

    if found_apis:
        print(f"  URLs de API/AJAX encontradas ({len(found_apis)}):")
        for u in list(found_apis)[:10]:
            print(f"    {u}")

    if found_json:
        print(f"  JSON embebido encontrado:")
        for name, data in found_json[:3]:
            print(f"    [{name}]: {data[:200]}")

    # Buscar selectores alternativos de productos
    alt_selectors = [
        (".product", "class=product"),
        ("[data-id-product]", "data-id-product"),
        (".js-product-miniature", "js-product-miniature"),
        ("li[class*='product']", "li[product class]"),
        (".thumbnail-container", "thumbnail-container"),
        (".product-description", "product-description"),
    ]
    print(f"\n  Selectores de producto encontrados en HTML:")
    for sel, name in alt_selectors:
        els = soup.select(sel)
        if els:
            print(f"    ✓ {name}: {len(els)} elementos")
            first = els[0]
            print(f"      Clases: {first.get('class', [])}")
            a = first.find("a")
            if a:
                print(f"      Link: {a.get('href', '')[:80]}")
        else:
            print(f"    ✗ {name}: 0")

    # Imprimir primeros 500 chars del body para ver estructura
    body = soup.find("body")
    if body:
        text = re.sub(r'\s+', ' ', body.get_text()[:800])
        print(f"\n  Texto body (primeros 800 chars):\n    {text[:800]}")

    # Buscar formularios o parámetros de paginación
    forms = soup.find_all("form")
    print(f"\n  Formularios: {len(forms)}")

    # Imprimir todo el HTML en chunks para buscar estructura
    print(f"\n  Primeros 2000 chars del HTML:")
    print(html[:2000])


print("=" * 65)
print("DIAGNÓSTICO FASE 3 — BÚSQUEDA DE API AJAX")
print("=" * 65)

# Inspeccionar categoría perros
url = f"{BASE}/es/alimento-para-perros/8-farmina-vet-life.html"
print(f"\n--- Categoría perros Vet Life ---")
r = fetch(url)
if r:
    inspect_html(r.text, "perros")

# Intentar con parámetros de AJAX típicos de PrestaShop
print(f"\n--- Intentos AJAX PrestaShop ---")
ajax_attempts = [
    f"{BASE}/es/alimento-para-perros/8-farmina-vet-life.html?ajax=1",
    f"{BASE}/es/alimento-para-perros/8-farmina-vet-life.html?id_category=8&n=100",
    f"{BASE}/index.php?controller=category&id_category=8&ajax=1",
    f"{BASE}/es/index.php?controller=category&id_category=8&ajax=1",
    f"{BASE}/api/products?category=8",
    f"{BASE}/es/busqueda?s=vet+life+cardiac",
    f"{BASE}/es/buscar?query=vet+life+cardiac",
]

for url in ajax_attempts:
    r = fetch(url, {"X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*"})
    if r:
        ct = r.headers.get("Content-Type", "")
        print(f"  Content-Type: {ct}")
        if "json" in ct:
            try:
                data = r.json()
                print(f"  JSON keys: {list(data.keys())[:10]}")
                print(f"  {json.dumps(data, ensure_ascii=False)[:500]}")
            except Exception:
                pass
        else:
            print(f"  Primeros 300 chars: {r.text[:300]}")

print("\nDiagnóstico completado.")
