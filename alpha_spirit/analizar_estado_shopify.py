#!/usr/bin/env python3
"""
Análisis del estado actual de productos Alpha Spirit en Shopify
===============================================================
Solo lectura. Muestra:
  1. Listado completo con variantes de cada producto
  2. Grupos de posibles duplicados no unificados (mismo producto, distintos IDs)
"""

import os
import re
import sys
import csv
import unicodedata
import requests
from pathlib import Path
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

SHOP_DOMAIN   = os.getenv("SHOP_DOMAIN",   "7ev1zx-eg.myshopify.com")
CLIENT_ID     = os.getenv("CLIENT_ID",     "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
VENDOR        = "Alpha Spirit"
API_VERSION   = "2024-10"

# ─── Shopify Auth ──────────────────────────────────────────────────────────────

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

# ─── Normalización para detección de duplicados ───────────────────────────────

def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


# Expresiones que indican peso/tamaño/formato y deben eliminarse del título base
_WEIGHT_RE = re.compile(
    r"""
    \b\d+\s*[x×]\s*\d+[\.,]?\d*\s*(?:kg|g|gr|lb|lbs|ml|l)\b  # 6x400g, 16x35g
    | \b\d+\s*[x×]\s*\d+\b                                      # 6x4uds, 16x4uds
    | \b\d+[\.,]?\d*\s*(?:kg|g|gr|lb|lbs|ml|l)\b               # 3kg, 400g, 2.5kg
    | \b\d+\s*uds?\b                                            # 25ud, 16uds
    | \b(?:individual|l|m|s|xl|xxl)\b                           # tallas de snacks
    """,
    re.VERBOSE | re.IGNORECASE,
)

_VENDOR_PREFIX_RE = re.compile(
    r"^(?:alpha\s+spirit\s+|alpha\s+the\s+|alpha\s+)",
    re.IGNORECASE,
)


def normalize_title(title: str) -> str:
    """Título base sin pesos ni tamaños, para agrupar posibles duplicados."""
    t = _strip_accents(title.lower())
    t = _VENDOR_PREFIX_RE.sub("", t)
    t = _WEIGHT_RE.sub(" ", t)
    # eliminar signos sueltos y espacios extra
    t = re.sub(r"[/\-_]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def variant_summary(product: dict) -> str:
    """Resumen compacto de las variantes de un producto."""
    variants = product.get("variants", [])
    if len(variants) == 1:
        opt = variants[0].get("option1", "")
        return f"1 variante ({opt})" if opt and opt.lower() != "default title" else "1 variante"

    option_name = (product.get("options") or [{}])[0].get("name", "")
    values = [v.get("option1", "") for v in variants]
    return f"{len(variants)} variantes [{option_name}: {', '.join(values)}]"


def variant_values(product: dict) -> list[str]:
    return [v.get("option1", "") for v in product.get("variants", [])]

# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("ERROR: Faltan credenciales")
        sys.exit(1)

    Path("resultados").mkdir(exist_ok=True)

    print("Obteniendo token Shopify...")
    token = get_token()
    print(f"Obteniendo productos vendor='{VENDOR}'...")
    products = get_products(token)
    print(f"Total productos: {len(products)}\n")

    # Ordenar por título para facilitar la lectura
    products.sort(key=lambda p: p["title"])

    lines = [
        "=" * 80,
        f"ESTADO SHOPIFY — {VENDOR}",
        f"Total productos: {len(products)}",
        "=" * 80,
        "",
    ]

    # ── 1. Listado completo ─────────────────────────────────────────────────────
    lines.append("─" * 80)
    lines.append("LISTADO COMPLETO")
    lines.append("─" * 80)

    multi_variant = []
    single_variant = []

    for p in products:
        pid      = p["id"]
        title    = p["title"]
        variants = p.get("variants", [])
        summary  = variant_summary(p)
        images   = p.get("images", [])
        n_imgs   = len(images)

        line = f"  [{pid}] {title}"
        lines.append(line)
        lines.append(f"         {summary}  |  {n_imgs} imagen(es)")

        if len(variants) > 1:
            multi_variant.append(p)
        else:
            single_variant.append(p)

    lines.append("")
    lines.append(f"  Productos con múltiples variantes (ya unificados) : {len(multi_variant)}")
    lines.append(f"  Productos con una sola variante                   : {len(single_variant)}")

    # ── 2. Detalle de productos ya unificados ───────────────────────────────────
    lines += [
        "",
        "─" * 80,
        "PRODUCTOS YA UNIFICADOS (≥2 variantes)",
        "─" * 80,
    ]
    for p in sorted(multi_variant, key=lambda p: p["title"]):
        lines.append(f"  [{p['id']}] {p['title']}")
        lines.append(f"         {variant_summary(p)}")

    # ── 3. Detección de posibles duplicados ────────────────────────────────────
    groups: dict[str, list] = defaultdict(list)
    for p in products:
        key = normalize_title(p["title"])
        groups[key].append(p)

    duplicate_groups = {k: v for k, v in groups.items() if len(v) > 1}

    lines += [
        "",
        "─" * 80,
        f"POSIBLES DUPLICADOS NO UNIFICADOS ({len(duplicate_groups)} grupos)",
        "─" * 80,
    ]

    if not duplicate_groups:
        lines.append("  ✓ No se detectaron duplicados.")
    else:
        for norm_title, group in sorted(duplicate_groups.items()):
            lines.append(f"\n  Título base: «{norm_title}»")
            for p in group:
                lines.append(f"    [{p['id']}] {p['title']}")
                lines.append(f"             {variant_summary(p)}")

    # ── 4. Productos de un solo variant con títulos muy similares ───────────────
    # Comparación por pares entre single-variant para detectar los que solo
    # difieren en peso/talla pero tienen títulos distintos (falsos negativos del
    # paso anterior porque el peso está embebido de forma diferente)
    lines += [
        "",
        "─" * 80,
        "SINGLE-VARIANT CON TÍTULOS MUY PARECIDOS (Jaccard ≥ 0.80 sobre tokens)",
        "─" * 80,
    ]

    def title_tokens(title: str) -> set:
        t = _strip_accents(title.lower())
        t = _WEIGHT_RE.sub(" ", t)
        t = re.sub(r"[/\-_]+", " ", t)
        tokens = set(re.split(r"\s+", t.strip()))
        stopwords = {"alpha", "spirit", "the", "de", "y", "con", "para", "a"}
        return tokens - stopwords - {""}

    similar_pairs = []
    sv = single_variant
    for i in range(len(sv)):
        for j in range(i + 1, len(sv)):
            a, b = sv[i], sv[j]
            ta = title_tokens(a["title"])
            tb = title_tokens(b["title"])
            union = ta | tb
            inter = ta & tb
            if not union:
                continue
            score = len(inter) / len(union)
            if score >= 0.80:
                similar_pairs.append((score, a, b))

    similar_pairs.sort(key=lambda x: x[0], reverse=True)

    if not similar_pairs:
        lines.append("  ✓ No se encontraron pares sospechosos.")
    else:
        for score, a, b in similar_pairs:
            lines.append(f"\n  Similitud {score:.0%}")
            lines.append(f"    [{a['id']}] {a['title']}")
            lines.append(f"             {variant_summary(a)}")
            lines.append(f"    [{b['id']}] {b['title']}")
            lines.append(f"             {variant_summary(b)}")

    # ── 5. Guardar resultados ───────────────────────────────────────────────────
    out_txt = Path("resultados/estado_shopify_alpha_spirit.txt")
    out_csv = Path("resultados/estado_shopify_alpha_spirit.csv")

    out_txt.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\nGuardado en {out_txt}")

    # CSV con una fila por producto
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "id", "title", "n_variantes", "variante_opcion",
            "valores_variantes", "n_imagenes", "titulo_normalizado",
        ])
        writer.writeheader()
        for p in products:
            opts = p.get("options") or [{}]
            writer.writerow({
                "id":               p["id"],
                "title":            p["title"],
                "n_variantes":      len(p.get("variants", [])),
                "variante_opcion":  opts[0].get("name", ""),
                "valores_variantes":"|".join(variant_values(p)),
                "n_imagenes":       len(p.get("images", [])),
                "titulo_normalizado": normalize_title(p["title"]),
            })

    print(f"CSV guardado en {out_csv}")


if __name__ == "__main__":
    main()
