#!/usr/bin/env python3
"""
Auditoría de imágenes Farmina Vet Life
=======================================
Usa el catálogo local de farmina.com y el matching mejorado para verificar
qué productos tienen la imagen correcta y cuáles necesitan corrección.
Genera reporte TXT y CSV con el resultado.
"""

import os
import re
import sys
import csv
import json
import requests
from pathlib import Path
from dotenv import load_dotenv

# Importar funciones de matching desde el procesador principal
sys.path.insert(0, str(Path(__file__).parent))
from process_farmina_vet_life import (
    find_best_match, build_farmina_catalog, SKIP_IDS
)

load_dotenv()

SHOP_DOMAIN  = os.getenv("SHOP_DOMAIN",  "7ev1zx-eg.myshopify.com")
CLIENT_ID    = os.getenv("CLIENT_ID",    "")
CLIENT_SECRET= os.getenv("CLIENT_SECRET","")
VENDOR       = "Farmina Vet Life"
API_VERSION  = "2024-10"

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
        raise ValueError(resp.text)
    return token


def get_products(token: str) -> list:
    h = {"X-Shopify-Access-Token": token}
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


def get_current_image(token: str, product_id: int) -> str:
    h = {"X-Shopify-Access-Token": token}
    resp = requests.get(
        f"https://{SHOP_DOMAIN}/admin/api/{API_VERSION}/products/{product_id}/images.json",
        headers=h, timeout=30,
    )
    resp.raise_for_status()
    images = resp.json().get("images", [])
    return images[0]["src"] if images else ""

# ─── Auditoría ────────────────────────────────────────────────────────────────

def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("ERROR: Faltan credenciales")
        sys.exit(1)

    Path("resultados").mkdir(exist_ok=True)

    print("Cargando catálogo farmina.com...")
    catalog = build_farmina_catalog()
    print(f"Catálogo: {len(catalog)} productos\n")

    print("Obteniendo token Shopify...")
    token = get_token()
    print("Obteniendo productos Vet Life...")
    products = get_products(token)
    print(f"Total: {len(products)} productos\n")

    rows = []
    lines = [
        "=" * 70,
        "AUDITORÍA FARMINA VET LIFE — MATCHING MEJORADO",
        "=" * 70,
    ]

    ok = wrong = sin_match = excluidos = 0

    for i, product in enumerate(products, 1):
        pid   = product["id"]
        title = product["title"]
        print(f"\n[{i}/{len(products)}] {title}")

        if pid in SKIP_IDS:
            print("  EXCLUIDO")
            excluidos += 1
            rows.append({"id": pid, "title": title, "estado": "EXCLUIDO",
                         "img_actual_fname": "", "match_nombre": "",
                         "match_img_fname": "", "match_url": "", "match_score": ""})
            continue

        # Imagen actual en Shopify
        current_src = get_current_image(token, pid)
        current_fname = current_src.split("?")[0].split("/")[-1] if current_src else ""
        print(f"  Img actual: {current_fname}")

        # Mejor match con el nuevo algoritmo
        match = find_best_match(title, catalog)

        if not match:
            print("  → SIN MATCH")
            sin_match += 1
            rows.append({"id": pid, "title": title, "estado": "SIN_MATCH",
                         "img_actual_fname": current_fname, "match_nombre": "",
                         "match_img_fname": "", "match_url": "", "match_score": ""})
            lines.append(f"\n[SIN_MATCH] {title}\n  ID: {pid}\n  Img actual: {current_fname}")
            continue

        match_fname = match["image_url"].split("/")[-1]
        match_score = round(
            len(_intersection(title, match)) / max(len(_union(title, match)), 1), 2
        )
        print(f"  Match: {match['name']} → {match_fname}")

        # Comparar: la imagen actual debería contener el ID del match de farmina
        farmina_id = match["id"]
        # La imagen actual fue generada como vetlife_{shopify_pid}_oficial.jpg
        # No podemos comparar nombres directamente; usamos el catálogo para ver si
        # el match de ahora difiere del match anterior (que usó el algoritmo antiguo)
        # Para detectar discrepancias reales necesitamos re-calcular con el match anterior.
        # Marcamos como REVISAR si el match_score < 0.3 (baja confianza)
        if match_score < 0.30:
            estado = "REVISAR"
            wrong += 1
        else:
            estado = "OK"
            ok += 1

        print(f"  → {estado} (score={match_score})")

        rows.append({
            "id": pid,
            "title": title,
            "estado": estado,
            "img_actual_fname": current_fname,
            "match_nombre": match["name"],
            "match_img_fname": match_fname,
            "match_url": match["image_url"],
            "match_score": match_score,
        })
        lines.append(
            f"\n[{estado}] {title}\n"
            f"  ID Shopify : {pid}\n"
            f"  Match      : {match['name']} (score={match_score})\n"
            f"  Img actual : {current_fname}\n"
            f"  Img match  : {match_fname}\n"
        )

    summary = (
        f"\n{'=' * 70}\n"
        f"RESUMEN\n"
        f"{'=' * 70}\n"
        f"  OK (confianza alta ≥0.30)  : {ok}\n"
        f"  REVISAR (confianza baja)   : {wrong}\n"
        f"  SIN MATCH                  : {sin_match}\n"
        f"  EXCLUIDOS                  : {excluidos}\n"
        f"  TOTAL                      : {len(products)}\n"
    )
    lines.append(summary)
    print(summary)

    Path("resultados/auditoria_vet_life.txt").write_text(
        "\n".join(lines), encoding="utf-8")

    with open("resultados/auditoria_vet_life.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "id", "title", "estado", "img_actual_fname",
            "match_nombre", "match_img_fname", "match_url", "match_score"])
        writer.writeheader()
        writer.writerows(rows)

    print("Reporte guardado en resultados/auditoria_vet_life.txt y .csv")


def _intersection(title, entry):
    from process_farmina_vet_life import _tokenize, _clean_shopify_title
    st = _tokenize(_clean_shopify_title(title))
    ft = _tokenize(entry["slug"] + " " + entry["name"])
    return st & ft

def _union(title, entry):
    from process_farmina_vet_life import _tokenize, _clean_shopify_title
    st = _tokenize(_clean_shopify_title(title))
    ft = _tokenize(entry["slug"] + " " + entry["name"])
    return st | ft


if __name__ == "__main__":
    main()
