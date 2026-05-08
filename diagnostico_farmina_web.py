#!/usr/bin/env python3
"""
Diagnóstico fase 6: Playwright obligatorio + screenshot + HTML renderizado
"""

import re
import sys
import time
import requests
from pathlib import Path
from bs4 import BeautifulSoup

BASE = "https://www.farmina.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

Path("resultados").mkdir(exist_ok=True)

print("=" * 65)
print("DIAGNÓSTICO FASE 6 — PLAYWRIGHT + SCREENSHOT + HTML COMPLETO")
print("=" * 65)

# ── 1. Playwright renderiza las páginas de categoría ──────────────────────
print("\n=== PLAYWRIGHT ===")
try:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="es-ES",
            extra_http_headers={"Accept-Language": "es-ES,es;q=0.9"},
        )
        page = ctx.new_page()

        for label, url in [
            ("DOG",  f"{BASE}/es/alimento-para-perros/8-farmina-vet-life.html"),
            ("CAT",  f"{BASE}/es/alimento-para-gatos/14-farmina-vet-life.html"),
            ("IT-DOG", f"{BASE}/it/alimento-per-cani/8-farmina-vet-life.html"),
        ]:
            print(f"\n--- {label}: {url} ---")
            try:
                page.goto(url, timeout=40000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)

                # Screenshot antes de aceptar cookies
                page.screenshot(
                    path=f"resultados/pw_{label}_antes.png", full_page=True)
                print(f"  Screenshot guardado: pw_{label}_antes.png")

                # Intentar aceptar cookies
                accepted = False
                cookie_selectors = [
                    "#CybotCookiebotDialogBodyButtonAccept",
                    "#accept-all", "#acceptAll",
                    "button[id*='accept']", "button[id*='Accept']",
                    "button[class*='accept']",
                    "button:text('Acepto')", "button:text('Aceptar todo')",
                    "button:text('Aceptar')", "button:text('Accetto')",
                    "button:text('Accept all')", "button:text('Accept')",
                    ".cc-btn.cc-allow", ".cookie-consent-accept",
                    "#onetrust-accept-btn-handler",
                    "[data-accept-all-cookies]",
                ]
                for sel in cookie_selectors:
                    try:
                        btn = page.locator(sel).first
                        if btn.is_visible(timeout=1500):
                            btn.click()
                            page.wait_for_timeout(3000)
                            accepted = True
                            print(f"  Cookies aceptadas con: {sel}")
                            break
                    except Exception:
                        pass

                if not accepted:
                    print("  No se encontró botón de cookies")
                    # Try pressing Escape or Tab to dismiss
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(1000)

                # Esperar a que carguen productos
                page.wait_for_timeout(5000)

                # Screenshot después
                page.screenshot(
                    path=f"resultados/pw_{label}_despues.png", full_page=True)
                print(f"  Screenshot guardado: pw_{label}_despues.png")

                # HTML renderizado
                html = page.content()
                html_path = f"resultados/pw_{label}_html.txt"
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(html)
                print(f"  HTML renderizado: {len(html)} chars → {html_path}")

                # Buscar links de producto en el HTML renderizado
                soup = BeautifulSoup(html, "html.parser")
                product_links = []
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    text = a.get_text(strip=True)
                    # URL de producto PrestaShop: /lang/category/ID-slug.html
                    # Excluir páginas informativas (/farmina/, /consumer/, /pet-care/)
                    if (re.search(r'/\d{3,}-[a-z]', href)
                            and href.endswith(".html")
                            and not any(x in href for x in [
                                "/farmina/", "/consumer/", "/pet-care/",
                                "/e-farmina/", "/genius", "/contact",
                            ])):
                        product_links.append((text[:60], href))

                print(f"  Links de producto encontrados: {len(product_links)}")
                for t, h in product_links[:30]:
                    print(f"    [{t}] → {h}")

                # Buscar imágenes de producto en el HTML
                imgs = []
                for img in soup.find_all("img"):
                    src = (img.get("src") or img.get("data-src") or
                           img.get("data-lazy-src") or "")
                    if src and any(ext in src for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                        if not any(x in src for x in ["logo", "icon", "sprite"]):
                            imgs.append(src)
                print(f"  Imágenes encontradas: {len(imgs)}")
                for s in imgs[:10]:
                    print(f"    {s}")

                # Mostrar primeros/últimos 1000 chars del BODY
                body_text = soup.body.get_text(" ", strip=True) if soup.body else ""
                print(f"\n  BODY TEXT (primeros 800 chars):")
                print(body_text[:800])

            except Exception as e:
                print(f"  ERROR: {e}")

        browser.close()

except ImportError:
    print("  Playwright no instalado")
except Exception as e:
    print(f"  ERROR Playwright general: {e}")

# ── 2. Prueba estática versión italiana ───────────────────────────────────
print("\n=== PRUEBA ESTÁTICA VERSIÓN ITALIANA ===")
it_url = f"{BASE}/it/alimento-per-cani/8-farmina-vet-life.html"
try:
    r = requests.get(it_url, headers=HEADERS, timeout=20)
    print(f"  GET {it_url} → {r.status_code} ({len(r.content)} bytes)")
    if r.status_code == 200:
        soup = BeautifulSoup(r.text, "html.parser")
        # Buscar productos en HTML estático
        product_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            if (re.search(r'/\d{3,}-[a-z]', href)
                    and href.endswith(".html")
                    and not any(x in href for x in [
                        "/farmina/", "/consumer/", "/pet-care/", "/e-farmina/"])):
                product_links.append((text[:60], href))
        print(f"  Links de producto: {len(product_links)}")
        for t, h in product_links[:20]:
            print(f"    [{t}] → {h}")
        # Body text
        body_text = soup.body.get_text(" ", strip=True) if soup.body else ""
        print(f"\n  BODY TEXT primeros 500 chars:")
        print(body_text[:500])
except Exception as e:
    print(f"  ERROR: {e}")

print("\nDiagnóstico completado.")
