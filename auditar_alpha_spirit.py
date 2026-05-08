#!/usr/bin/env python3
"""
Auditoría de imágenes Alpha Spirit
====================================
Usa el catálogo local de aspiritpetfood.store y el matching del procesador
para verificar qué productos tienen la imagen correcta y cuáles necesitan corrección.
Genera reporte TXT y CSV en resultados/.
"""

import os
import sys
import csv
import requests
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from process_alpha_spirit import (
    find_best_match, build_catalog, _tokenize,
)

load_dotenv()

SHOP_DOMAIN   = os.getenv("SHOP_DOMAIN",   "7ev1zx-eg.myshopify.com")
CLIENT_ID     = os.getenv("CLIENT_ID",     "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
VENDOR        = "Alpha Spirit"
API_VERSION   = "2024-10"

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

def _calc_score(shopify_title: str, entry: dict) -> float:
    st = _tokenize(shopify_title)
    ct = _tokenize(entry["title"] + " " + entry["handle"] + " " + entry.get("product_type", ""))
    if not ct:
        return 0.0
    inter = st & ct
    union = st | ct
    return len(inter) / len(union) if union else 0.0


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("ERROR: Faltan credenciales")
        sys.exit(1)

    Path("resultados").mkdir(exist_ok=True)

    print("Cargando catálogo aspiritpetfood.store...")
    catalog = build_catalog()
    print(f"Catálogo: {len(catalog)} productos\n")

    print("Obteniendo token Shopify...")
    token = get_token()
    print("Obteniendo productos Alpha Spirit...")
    products = get_products(token)
    print(f"Total: {len(products)} productos\n")

    rows = []
    lines = [
        "=" * 70,
        "AUDITORÍA ALPHA SPIRIT — MATCHING",
        "=" * 70,
    ]

    ok = wrong = sin_match = 0

    for i, product in enumerate(products, 1):
        pid   = product["id"]
        title = product["title"]
        print(f"\n[{i}/{len(products)}] {title}")

        current_src   = get_current_image(token, pid)
        current_fname = current_src.split("?")[0].split("/")[-1] if current_src else ""
        print(f"  Img actual: {current_fname}")

        match, score = find_best_match(title, catalog)

        if not match:
            print("  → SIN MATCH")
            sin_match += 1
            rows.append({
                "shopify_id":    pid,
                "shopify_title": title,
                "match_correcto": "",
                "match_handle":  "",
                "match_score":   f"{score:.2f}",
                "img_actual":    current_fname,
                "estado":        "SIN_MATCH",
            })
            lines.append(f"\n[SIN_MATCH] {title}\n  ID: {pid}")
            continue

        match_score = round(_calc_score(title, match), 2)
        estado = "OK" if match_score >= 0.25 else "REVISAR"
        if estado == "OK":
            ok += 1
        else:
            wrong += 1

        print(f"  Match: {match['title']} ({match['handle']}) score={match_score:.2f} → {estado}")

        rows.append({
            "shopify_id":    pid,
            "shopify_title": title,
            "match_correcto": match["title"],
            "match_handle":  match["handle"],
            "match_score":   f"{match_score:.2f}",
            "img_actual":    current_fname,
            "estado":        estado,
        })
        lines.append(
            f"\n[{estado}] {title}\n"
            f"  ID Shopify : {pid}\n"
            f"  Match      : {match['title']} ({match['handle']}) score={match_score:.2f}\n"
            f"  Img actual : {current_fname}\n"
        )

    summary = (
        f"\n{'=' * 70}\n"
        f"RESUMEN\n"
        f"{'=' * 70}\n"
        f"  OK (score ≥ 0.25)          : {ok}\n"
        f"  REVISAR (score < 0.25)     : {wrong}\n"
        f"  SIN MATCH                  : {sin_match}\n"
        f"  TOTAL                      : {len(products)}\n"
    )
    lines.append(summary)
    print(summary)

    Path("resultados/auditoria_alpha_spirit.txt").write_text(
        "\n".join(lines), encoding="utf-8")

    with open("resultados/auditoria_alpha_spirit.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "shopify_id", "shopify_title", "match_correcto",
            "match_handle", "match_score", "img_actual", "estado"])
        writer.writeheader()
        writer.writerows(rows)

    print("Reporte guardado en resultados/auditoria_alpha_spirit.txt y .csv")


if __name__ == "__main__":
    main()
