#!/usr/bin/env python3
"""
Diagnóstico fase 5: sitemap.xml + HTML completo + Playwright
"""

import re
import sys
import json
import time
import requests
from bs4 import BeautifulSoup
from xml.etree import ElementTree as ET

BASE = "https://www.farmina.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def fetch(url, timeout=20):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        print(f"  GET {url[:90]} → {r.status_code} ({len(r.content)} bytes)")
        return r if r.status_code == 200 else None
    except Exception as e:
        print(f"  ERROR {url[:70]}: {e}")
        return None

print("=" * 65)
print("DIAGNÓSTICO FASE 5 — SITEMAP + HTML COMPLETO + PLAYWRIGHT")
print("=" * 65)

vet_life_product_urls = []

# ── 1. Sitemap XML ──────────────────────────────────────────────────────────
print("\n=== 1. SITEMAP XML ===")
sitemap_candidates = [
    f"{BASE}/sitemap.xml",
    f"{BASE}/sitemap_index.xml",
    f"{BASE}/es/sitemap.xml",
    f"{BASE}/es/sitemap_1.xml",
    f"{BASE}/sitemaps/sitemap.xml",
    f"{BASE}/sitemap-products.xml",
    f"{BASE}/robots.txt",
]

for url in sitemap_candidates:
    r = fetch(url)
    if not r:
        continue

    text = r.text

    # robots.txt → buscar Sitemap: líneas
    if "robots.txt" in url:
        for line in text.splitlines():
            if line.lower().startswith("sitemap:"):
                sm_url = line.split(":", 1)[1].strip()
                print(f"  robots.txt → Sitemap: {sm_url}")
                sitemap_candidates.append(sm_url)
        continue

    # Intentar parsear XML
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as e:
        print(f"  No es XML válido: {e}")
        print(f"  Primeros 400 chars: {text[:400]}")
        continue

    ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""

    # Sitemap index → añadir sub-sitemaps a la lista
    sub_sitemaps = [e.text for e in root.iter(f"{ns}loc")
                    if e.text and "sitemap" in e.text.lower()]
    if sub_sitemaps:
        print(f"  Sitemap index — {len(sub_sitemaps)} sub-sitemaps:")
        for s in sub_sitemaps[:10]:
            print(f"    {s}")
        sitemap_candidates.extend(sub_sitemaps)
        continue

    # Sitemap normal → extraer URLs
    locs = [e.text for e in root.iter(f"{ns}loc") if e.text]
    print(f"  Total URLs: {len(locs)}")
    vl = [u for u in locs if "vet" in u.lower() or "vet-life" in u.lower()]
    print(f"  URLs Vet Life: {len(vl)}")
    for u in vl[:30]:
        print(f"    {u}")
    vet_life_product_urls.extend(vl)

    print(f"\n  Primeras 10 URLs:")
    for u in locs[:10]:
        print(f"    {u}")

    if locs:
        break  # sitemap válido encontrado

# ── 2. HTML completo — buscar datos de producto en JS ──────────────────────
if not vet_life_product_urls:
    print("\n=== 2. HTML COMPLETO — BÚSQUEDA DE DATOS EN JS ===")
    cat_urls = [
        f"{BASE}/es/alimento-para-perros/8-farmina-vet-life.html",
        f"{BASE}/es/alimento-para-gatos/14-farmina-vet-life.html",
    ]
    for cat_url in cat_urls:
        r = fetch(cat_url)
        if not r:
            continue
        html = r.text
        print(f"\n  HTML total: {len(html)} chars")

        # Buscar bloques JSON con datos de producto
        json_blocks = re.findall(r'\{[^{}]*"id_product"[^{}]*\}', html)
        if json_blocks:
            print(f"  Bloques JSON con id_product: {len(json_blocks)}")
            for b in json_blocks[:5]:
                print(f"    {b[:200]}")

        # Buscar variables JS con arrays de producto
        js_arrays = re.findall(
            r'(?:products|items|listings)\s*[=:]\s*(\[.*?\])',
            html, re.DOTALL
        )
        if js_arrays:
            print(f"  Arrays JS de productos: {len(js_arrays)}")
            for a in js_arrays[:3]:
                print(f"    {a[:300]}")

        # Buscar data-id-product o href con patrón /number-slug.html
        soup = BeautifulSoup(html, "html.parser")
        # data-id-product
        for el in soup.find_all(attrs={"data-id-product": True}):
            print(f"  data-id-product: {el.get('data-id-product')} href={el.get('href','')}")

        # Todos los <a> con href que sigan patrón /NNN-slug.html
        product_hrefs = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if re.search(r'/\d{3,}-[a-z]', href) and href.endswith(".html"):
                if "farmina" in href and "vet" in href.lower():
                    product_hrefs.append(href)
        if product_hrefs:
            print(f"  Links con patrón /NNN-slug.html (vet): {len(product_hrefs)}")
            for h in product_hrefs[:20]:
                print(f"    {h}")
            vet_life_product_urls.extend(product_hrefs)

        # Mostrar últimos 2000 chars (donde suelen estar los productos)
        print(f"\n  ÚLTIMOS 2000 chars:")
        print(html[-2000:])

# ── 3. Playwright — renderizado real con JS ────────────────────────────────
if not vet_life_product_urls:
    print("\n=== 3. PLAYWRIGHT — RENDERIZADO CON JS ===")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_extra_http_headers(HEADERS)

            target = f"{BASE}/es/alimento-para-perros/8-farmina-vet-life.html"
            print(f"  Navegando a {target}")
            page.goto(target, timeout=30000, wait_until="networkidle")

            # Aceptar cookies si hay botón
            for sel in ["#CybotCookiebotDialogBodyButtonAccept",
                        "button[id*='accept']", "button[id*='cookie']",
                        ".cookie-accept", "#accept-cookies",
                        "button:has-text('Acepto')", "button:has-text('Aceptar')"]:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=2000):
                        btn.click()
                        print(f"  Cookies aceptadas: {sel}")
                        page.wait_for_timeout(2000)
                        break
                except Exception:
                    pass

            # Esperar productos
            page.wait_for_timeout(5000)

            html = page.content()
            print(f"  HTML tras JS: {len(html)} chars")

            soup = BeautifulSoup(html, "html.parser")
            product_links = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if re.search(r'/\d+-[a-z]', href) and href.endswith(".html"):
                    text = a.get_text(strip=True)
                    if text and len(text) > 3:
                        product_links.append((text[:60], href))

            print(f"  Links de producto: {len(product_links)}")
            for t, h in product_links[:30]:
                print(f"    [{t}] → {h}")
                vet_life_product_urls.append(h)

            # Screenshot para depuración
            page.screenshot(path="resultados/farmina_playwright.png")
            print("  Screenshot guardado en resultados/farmina_playwright.png")

            browser.close()

    except ImportError:
        print("  Playwright no instalado — instalar con: pip install playwright && playwright install chromium")
    except Exception as e:
        print(f"  ERROR Playwright: {e}")

# ── 4. Probar primer producto encontrado ───────────────────────────────────
if vet_life_product_urls:
    print(f"\n=== 4. PRUEBA PRODUCTO INDIVIDUAL ===")
    url = vet_life_product_urls[0]
    full = url if url.startswith("http") else BASE + url
    r = fetch(full)
    if r:
        soup = BeautifulSoup(r.text, "html.parser")
        print(f"  Título: {soup.title.text.strip() if soup.title else 'N/A'}")

        # Imágenes grandes
        imgs = []
        for img in soup.find_all("img"):
            src = (img.get("data-zoom-image") or img.get("data-src") or
                   img.get("src") or "")
            if src and any(ext in src for ext in [".jpg", ".jpeg", ".png"]):
                w = int(img.get("width", 0) or 0)
                imgs.append((w, src))
        imgs.sort(reverse=True)
        print(f"  Imágenes: {len(imgs)}")
        for w, s in imgs[:10]:
            print(f"    {w}px: {s}")

        # URLs en scripts
        for script in soup.find_all("script"):
            text = script.string or ""
            if "image" in text.lower() and len(text) > 100:
                urls = re.findall(r'https?://[^\s"\']+\.(?:jpg|jpeg|png)', text)
                if urls:
                    print(f"  URLs en scripts: {urls[:5]}")
                    break

        # Últimos 3000 chars del HTML
        print(f"\n  ÚLTIMOS 3000 chars del HTML del producto:")
        print(r.text[-3000:])
else:
    print("\n  No se encontraron URLs de productos Vet Life en ninguna fuente.")

print("\nDiagnóstico completado.")
