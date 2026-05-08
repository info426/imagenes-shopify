#!/usr/bin/env python3
"""
Auditoría de imágenes Farmina Vet Life
=======================================
Para cada producto Shopify:
1. Busca en DuckDuckGo el título → obtiene URL correcta en farmina.com
2. Extrae el ID de producto farmina y la imagen /fotoprodotti/
3. Compara con la imagen actual en Shopify
4. Genera reporte CSV con coincidencias y discrepancias
"""

import os
import re
import sys
import csv
import time
import json
import requests
from pathlib import Path
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

SHOP_DOMAIN   = os.getenv("SHOP_DOMAIN",   "7ev1zx-eg.myshopify.com")
CLIENT_ID     = os.getenv("CLIENT_ID",     "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
VENDOR        = "Farmina Vet Life"
API_VERSION   = "2024-10"
FARMINA_BASE  = "https://www.farmina.com"
CATALOG_FILE  = Path("resultados/farmina_catalog.json")

SKIP_IDS = {
    15509747827075,  # DOG CARDIAC (prueba inicial)
    15509749924227,  # CAT HEPATIC 12X85GR (ya trabajado manualmente)
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

# ─── Shopify API ──────────────────────────────────────────────────────────────

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


def get_products(token: str) -> list:
    h = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    products, params = [], {"limit": 250, "vendor": VENDOR}
    url = f"https://{SHOP_DOMAIN}/admin/api/{API_VERSION}/products.json"
    while url:
        resp = requests.get(url, headers=h, params=params, timeout=30)
        resp.raise_for_status()
        products.extend(resp.json().get("products", []))
        params, url = {}, None
        link = resp.headers.get("Link", "")
        if 'rel="next"' in link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.strip().split(";")[0].strip("<>")
    return products


def get_images(token: str, product_id: int) -> list:
    h = {"X-Shopify-Access-Token": token}
    resp = requests.get(
        f"https://{SHOP_DOMAIN}/admin/api/{API_VERSION}/products/{product_id}/images.json",
        headers=h, timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("images", [])

# ─── Búsqueda web ─────────────────────────────────────────────────────────────

def search_farmina_url(title: str) -> str | None:
    """Busca en DuckDuckGo el producto y devuelve la URL de farmina.com/es."""
    try:
        from duckduckgo_search import DDGS
        query = f"{title} site:farmina.com/es"
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
        for r in results:
            url = r.get("href", "")
            if "farmina.com" in url and "/eshop/" in url:
                print(f"    DDG → {url}")
                return url
        # Segundo intento sin site: filter
        query2 = f"{title} farmina.com vet life"
        with DDGS() as ddgs:
            results2 = list(ddgs.text(query2, max_results=5))
        for r in results2:
            url = r.get("href", "")
            if "farmina.com" in url and "/eshop/" in url:
                print(f"    DDG2 → {url}")
                return url
    except Exception as e:
        print(f"    DDG ERROR: {e}")
    return None


def extract_farmina_id_from_url(url: str) -> str | None:
    """Extrae el ID numérico del producto desde la URL farmina.com."""
    m = re.search(r'/(\d+)-[^/]+\.html', url)
    return m.group(1) if m else None


def get_image_from_catalog(farmina_id: str, catalog: list) -> str | None:
    """Busca la imagen en el catálogo por ID de farmina.com."""
    for entry in catalog:
        if entry["id"] == farmina_id:
            return entry["image_url"]
    return None


def get_image_from_page(url: str) -> str | None:
    """Obtiene la imagen del producto desde la página farmina.com (HTML estático)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if "fotoprodotti" in src:
                return src if src.startswith("http") else FARMINA_BASE + src
    except Exception:
        pass
    return None

# ─── Auditoría ────────────────────────────────────────────────────────────────

def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("ERROR: Faltan CLIENT_ID o CLIENT_SECRET")
        sys.exit(1)

    Path("resultados").mkdir(exist_ok=True)

    if CATALOG_FILE.exists():
        catalog = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
        print(f"Catálogo cargado: {len(catalog)} productos")
    else:
        catalog = []
        print("AVISO: catálogo no encontrado, solo se usará búsqueda web")

    print("Obteniendo token Shopify...")
    token = get_token()
    print("Obteniendo productos Vet Life...")
    products = get_products(token)
    print(f"Total: {len(products)} productos\n")

    rows = []
    report_lines = []

    report_lines.append("=" * 70)
    report_lines.append("AUDITORÍA FARMINA VET LIFE — IMAGEN ACTUAL vs IMAGEN CORRECTA")
    report_lines.append("=" * 70)

    ok_count = wrong_count = skip_count = nomatch_count = 0

    for i, product in enumerate(products, 1):
        pid   = product["id"]
        title = product["title"]
        print(f"\n[{i}/{len(products)}] {title}  (ID: {pid})")

        if pid in SKIP_IDS:
            print(f"  EXCLUIDO")
            skip_count += 1
            rows.append({
                "id": pid, "title": title,
                "estado": "EXCLUIDO",
                "imagen_actual": "", "imagen_correcta": "",
                "url_farmina": "",
            })
            continue

        # Imagen actual en Shopify
        images = get_images(token, pid)
        current_img = images[0]["src"] if images else ""
        current_fname = current_img.split("?")[0].split("/")[-1] if current_img else ""
        print(f"  Imagen actual: {current_fname}")

        # Buscar URL correcta en farmina.com
        time.sleep(1.5)  # respetar rate limit DuckDuckGo
        farmina_url = search_farmina_url(title)

        correct_img = None
        farmina_id  = None

        if farmina_url:
            farmina_id = extract_farmina_id_from_url(farmina_url)
            print(f"  ID farmina: {farmina_id}")

            # Buscar imagen en catálogo por ID
            if farmina_id:
                correct_img = get_image_from_catalog(farmina_id, catalog)

            # Si no está en catálogo, intentar obtenerla de la página
            if not correct_img:
                correct_img = get_image_from_page(farmina_url)

        correct_fname = correct_img.split("/")[-1] if correct_img else "NO ENCONTRADA"
        print(f"  Imagen correcta: {correct_fname}")

        # Comparar
        if not correct_img:
            estado = "SIN_MATCH_WEB"
            nomatch_count += 1
        elif correct_fname in current_fname or current_fname in correct_fname:
            estado = "OK"
            ok_count += 1
        else:
            estado = "DISCREPANCIA"
            wrong_count += 1

        print(f"  → {estado}")

        line = (
            f"\n[{estado}] {title}\n"
            f"  ID Shopify  : {pid}\n"
            f"  URL farmina : {farmina_url or 'N/A'}\n"
            f"  Img actual  : {current_fname}\n"
            f"  Img correcta: {correct_fname}\n"
        )
        report_lines.append(line)

        rows.append({
            "id": pid,
            "title": title,
            "estado": estado,
            "imagen_actual": current_fname,
            "imagen_correcta": correct_fname,
            "url_farmina": farmina_url or "",
            "farmina_id": farmina_id or "",
            "imagen_correcta_url": correct_img or "",
        })

        time.sleep(0.5)

    # Resumen
    summary = (
        f"\n{'=' * 70}\n"
        f"RESUMEN\n"
        f"{'=' * 70}\n"
        f"  OK (imagen correcta)   : {ok_count}\n"
        f"  DISCREPANCIA           : {wrong_count}\n"
        f"  SIN MATCH WEB          : {nomatch_count}\n"
        f"  EXCLUIDOS              : {skip_count}\n"
        f"  TOTAL                  : {len(products)}\n"
    )
    report_lines.append(summary)
    print(summary)

    # Guardar reporte de texto
    report_path = Path("resultados/auditoria_vet_life.txt")
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    print(f"Reporte guardado: {report_path}")

    # Guardar CSV con datos para corrección
    csv_path = Path("resultados/auditoria_vet_life.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        fieldnames = ["id", "title", "estado", "imagen_actual",
                      "imagen_correcta", "url_farmina", "farmina_id",
                      "imagen_correcta_url"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"CSV guardado: {csv_path}")


if __name__ == "__main__":
    main()
