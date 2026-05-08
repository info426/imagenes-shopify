#!/usr/bin/env python3
"""
Análisis de productos Alpha Spirit en Shopify vs catálogo aspiritpetfood.store
===============================================================================
Solo lectura — no modifica nada en Shopify.
Muestra el matching entre productos Shopify y catálogo oficial.
"""

import os
import re
import sys
import json
import time
import unicodedata
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

SHOP_DOMAIN   = os.getenv("SHOP_DOMAIN",   "7ev1zx-eg.myshopify.com")
CLIENT_ID     = os.getenv("CLIENT_ID",     "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
VENDOR        = "Alpha Spirit"
API_VERSION   = "2024-10"
CATALOG_FILE  = Path("resultados/alpha_spirit_catalog.json")

STORE_BASE    = "https://www.aspiritpetfood.store"
COLLECTION_SLUG = "alpha-spirit"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

# ─── Shopify Auth ─────────────────────────────────────────────────────────────

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


def get_shopify_products(token: str) -> list:
    headers = {"X-Shopify-Access-Token": token}
    products, params = [], {"limit": 250, "vendor": VENDOR}
    url = f"https://{SHOP_DOMAIN}/admin/api/{API_VERSION}/products.json"
    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        products.extend(resp.json().get("products", []))
        params, url = {}, None
        link = resp.headers.get("Link", "")
        if 'rel="next"' in link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.strip().split(";")[0].strip("<>")
    return products

# ─── Catálogo aspiritpetfood.store ───────────────────────────────────────────

def build_catalog() -> list:
    if CATALOG_FILE.exists():
        catalog = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
        print(f"Catálogo cargado desde caché: {len(catalog)} productos")
        return catalog

    print("Construyendo catálogo con Playwright...")
    from playwright.sync_api import sync_playwright

    catalog = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="es-ES",
            extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]},
        )
        page = ctx.new_page()

        page_num = 1
        while True:
            url = (f"{STORE_BASE}/collections/{COLLECTION_SLUG}"
                   f"/products.json?limit=250&page={page_num}")
            print(f"  Cargando página {page_num}: {url}")
            page.goto(url, timeout=40000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            raw = page.evaluate("() => document.body.innerText")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                print(f"  No se pudo parsear JSON en página {page_num} — parando")
                break

            products = data.get("products", [])
            if not products:
                print(f"  Página {page_num} vacía — fin del catálogo")
                break

            print(f"  Página {page_num}: {len(products)} productos")
            for p in products:
                images = p.get("images", [])
                catalog.append({
                    "id":           p["id"],
                    "title":        p["title"],
                    "handle":       p["handle"],
                    "vendor":       p.get("vendor", ""),
                    "product_type": p.get("product_type", ""),
                    "image_url":    images[0]["src"] if images else "",
                    "tags":         p.get("tags", []),
                })

            page_num += 1
            time.sleep(1)

        browser.close()

    # Deduplicar por handle
    seen = set()
    deduped = []
    for entry in catalog:
        if entry["handle"] not in seen:
            seen.add(entry["handle"])
            deduped.append(entry)

    CATALOG_FILE.parent.mkdir(exist_ok=True)
    CATALOG_FILE.write_text(
        json.dumps(deduped, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Catálogo guardado: {len(deduped)} productos → {CATALOG_FILE}")
    return deduped

# ─── Algoritmo de matching ────────────────────────────────────────────────────

def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )

_ES_TO_EN = {
    "pollo":      "chicken",
    "pescado":    "fish",
    "cordero":    "lamb",
    "pato":       "duck",
    "ternera":    "beef",
    "conejo":     "rabbit",
    "salmon":     "salmon",
    "trucha":     "trout",
    "atun":       "tuna",
    "dorada":     "seabream",
    "jamon":      "ham",
    "pavo":       "turkey",
    "cerdo":      "pork",
    "vaca":       "beef",
    "buey":       "beef",
    "ciervo":     "venison",
    "jabali":     "boar",
    "semihúmedo": "semiwet",
    "semihumedo": "semiwet",
    "humedo":     "wet",
    "caja":       "wet",
    "tarro":      "wet",
    "seco":       "dry",
    "cachorro":   "puppy",
    "cachorros":  "puppies",
    "adulto":     "adult",
    "adultos":    "adult",
    "snack":      "snack",
    "snacks":     "snack",
}

_STOPWORDS = {
    "the", "and", "for", "con", "para", "del", "los", "las", "una", "que",
    "mas", "food", "alimento", "diet", "de", "la", "el", "en", "y",
}
_IGNORE_TOKENS = {
    "alpha", "spirit", "alphaspirit", "aspiritpetfood",
    "500g", "2kg", "9kg", "14kg", "35g", "85g", "300g", "150g",
    "gr", "kg", "g", "x", "pack",
}


def _preprocess(text: str) -> str:
    t = _strip_accents(text.lower())
    t = re.sub(r'semi[\s\-]?h[uú]medo', 'semiwet', t)
    t = re.sub(r'\d+[x×]\d+\w*', '', t)
    t = re.sub(r'\d+[\.,]?\d*\s*(kg|g|gr|l|ml|tab|und)\b', '', t,
               flags=re.IGNORECASE)
    t = re.sub(r'\(.*?\)', '', t)
    # aplicar traducción ES→EN
    words = re.split(r'(\s+)', t)
    t = "".join(_ES_TO_EN.get(w.strip(), w) for w in words)
    return t


def _tokenize(text: str) -> set:
    t = _preprocess(text)
    tokens = set(re.split(r'[\s\-_&+•\./,]+', t))
    return tokens - _STOPWORDS - _IGNORE_TOKENS - {''}


def _is_wet(title: str) -> bool:
    t = title.upper()
    return any(x in t for x in ["CAJA", "TARRO", "HUMEDO", "HÚMEDO",
                                 "WET", "MOUSSE", "PATE", "PATÉ",
                                 "ESTOFADO", "SALCHICHA"])


def _is_semiwet(title: str) -> bool:
    t = title.upper()
    return any(x in t for x in ["SEMI", "SEMI-HUMEDO", "SEMI-HÚMEDO", "SEMIWET"])


def _is_snack(title: str) -> bool:
    t = title.lower()
    return any(x in t for x in ["snack", "barrita", "bocadito", "hueso",
                                 "premio", "treat", "lonchita"])


def _score(shopify_title: str, entry: dict) -> float:
    shopify_tokens = _tokenize(shopify_title)
    catalog_text   = entry["title"] + " " + entry["handle"] + " " + entry.get("product_type", "")
    catalog_tokens = _tokenize(catalog_text)
    if not catalog_tokens:
        return 0.0
    inter = shopify_tokens & catalog_tokens
    union = shopify_tokens | catalog_tokens
    return len(inter) / len(union) if union else 0.0


def find_best_match(shopify_title: str, catalog: list) -> tuple[dict | None, float]:
    title_lower = shopify_title.lower()

    # Filtrar por especie
    if any(w in title_lower for w in ["perro", "canino", "dog"]):
        species_tag = "dog"
    elif any(w in title_lower for w in ["gato", "felino", "cat"]):
        species_tag = "cat"
    else:
        species_tag = None

    if species_tag:
        candidates = [e for e in catalog
                      if species_tag in e["handle"].lower()
                      or species_tag in e["title"].lower()
                      or any(species_tag in t.lower() for t in e.get("tags", []))]
        if not candidates:
            candidates = catalog
    else:
        candidates = catalog

    # Separar por tipo de producto
    is_snack   = _is_snack(shopify_title)
    is_wet     = _is_wet(shopify_title)
    is_semiwet = _is_semiwet(shopify_title)

    if is_snack:
        snack_candidates = [e for e in candidates
                            if _is_snack(e["title"]) or _is_snack(e["handle"])]
        if snack_candidates:
            candidates = snack_candidates
    elif is_semiwet:
        semiwet_candidates = [e for e in candidates
                              if _is_semiwet(e["title"]) or _is_semiwet(e["handle"])
                              or "semi" in e["handle"].lower()]
        if semiwet_candidates:
            candidates = semiwet_candidates
    elif is_wet:
        wet_candidates = [e for e in candidates
                          if _is_wet(e["title"]) or _is_wet(e["handle"])
                          or "wet" in e["handle"].lower() or "humedo" in e["handle"].lower()]
        if wet_candidates:
            candidates = wet_candidates

    scored = sorted(
        [(e, _score(shopify_title, e)) for e in candidates],
        key=lambda x: x[1], reverse=True,
    )

    if not scored:
        return None, 0.0

    best_entry, best_score = scored[0]
    return (best_entry, best_score) if best_score >= 0.10 else (None, best_score)

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("ANÁLISIS ALPHA SPIRIT — SHOPIFY vs aspiritpetfood.store")
    print("=" * 70)

    Path("resultados").mkdir(exist_ok=True)

    token    = get_token()
    products = get_shopify_products(token)
    print(f"\nProductos '{VENDOR}' en Shopify: {len(products)}\n")

    if not products:
        print("No se encontraron productos con ese vendor. Verifica el nombre exacto.")
        sys.exit(1)

    catalog = build_catalog()
    print(f"\nCatálogo Alpha Spirit (aspiritpetfood.store): {len(catalog)} productos\n")

    print(f"\n{'#':<4} {'SHOPIFY TITLE':<50} {'MATCH OFICIAL':<40} {'SCORE'}")
    print("-" * 105)

    sin_match   = []
    con_match   = []
    score_bajos = []

    for i, product in enumerate(products, 1):
        pid   = product["id"]
        title = product["title"]
        match, score = find_best_match(title, catalog)

        short_title = title[:49]
        if match:
            match_name = match["title"][:39]
            flag = "✓" if score >= 0.25 else "~"
            print(f"{i:<4} {short_title:<50} {match_name:<40} {flag} {score:.2f}")
            con_match.append((product, match, score))
            if score < 0.25:
                score_bajos.append((product, match, score))
        else:
            print(f"{i:<4} {short_title:<50} {'— SIN MATCH —':<40}   {score:.2f}")
            sin_match.append(product)

    print("\n" + "=" * 70)
    print("RESUMEN")
    print("=" * 70)
    print(f"  Productos en Shopify         : {len(products)}")
    print(f"  Con match (score ≥ 0.10)     : {len(con_match)}")
    print(f"    - Score alto  (≥ 0.25)     : {len(con_match) - len(score_bajos)}")
    print(f"    - Score bajo  (0.10-0.24)  : {len(score_bajos)}")
    print(f"  Sin match                    : {len(sin_match)}")

    if score_bajos:
        print("\nMatches con score bajo (revisar manualmente):")
        for product, match, score in score_bajos:
            print(f"  [{score:.2f}] {product['title']}")
            print(f"         → {match['title']} ({match['handle']})")

    if sin_match:
        print("\nSin match:")
        for p in sin_match:
            print(f"  ID {p['id']} · {p['title']}")

    print("\nAnálisis completado.")
    print(f"Catálogo guardado en: {CATALOG_FILE}")


if __name__ == "__main__":
    main()
