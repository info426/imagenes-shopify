"""
Scraper para Artero — artero.com/es/petcare/

El listado se carga vía JS y no es fiable de scrapear. En su lugar usamos
una estrategia híbrida que busca la URL de cada producto bajo demanda:

  1. Match Jaccard contra el catálogo ya cacheado
  2. Construir slug a partir del título Shopify y probar la URL directa
     (ARTERO ACONDICIONADOR FLASH 300 ML → artero-acondicionador-flash-300ml)
  3. Fallback: DuckDuckGo con filtro site:artero.com/es/petcare/
  4. Cuando se encuentra una URL válida → cachear en resultados/artero_catalog.json

Interfaz estándar (requerida por core/process_brand.py):
  scrape_catalog(web_url, rebuild=False) -> dict
  find_best_match(shopify_title, catalog) -> (handle, score)
"""

import atexit
import json
import logging
import re
import time
import unicodedata
from pathlib import Path

log = logging.getLogger(__name__)

CATALOG_PATH = Path("resultados/artero_catalog.json")
BASE_URL     = "https://artero.com/es/petcare"
BASE_DOMAIN  = "artero.com"
MIN_SCORE    = 0.10

# Marcadores internos que añade la tienda a los títulos
_TITLE_NOISE = re.compile(r'\s*\((?:NDR|PV|NV|ONLINE)\)\s*', re.IGNORECASE)

IGNORE_TOKENS = {
    "artero", "professional", "pet",
    "de", "el", "la", "los", "las", "con", "sin", "y", "para",
    "ml", "gr", "g", "kg", "l", "x", "oz", "ud", "uds",
    "ndr", "pv", "nv", "online",
    "a", "e", "o",
}

# Slugs de título Shopify → slug real de artero.com cuando difieren
# (el título Shopify incluye categorías o descripciones que la web omite)
_SLUG_CORRECTIONS: dict[str, str] = {
    "artero-higiene-perfume-violet-90ml":    "artero-perfume-violet-90ml",
    "artero-espuma-acondicionador-zoom":     "artero-espuma-voluminizadora-zoom-150ml",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Estado Playwright reutilizado entre llamadas a find_best_match()
_PW = {"pw": None, "browser": None, "ctx": None, "page": None}


# ─── Utilidades de texto ──────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9\s]", " ", text)


def _clean_title(title: str) -> str:
    return _TITLE_NOISE.sub(" ", title).strip()


def _title_to_slug(title: str) -> str:
    """ARTERO ACONDICIONADOR FLASH 300 ML (NDR) → artero-acondicionador-flash-300ml"""
    norm = _normalize(_clean_title(title))
    # "300 ml" → "300ml" (la web concatena número y unidad)
    norm = re.sub(r'(\d+)\s+(ml|gr|kg|g|l|oz)\b', r'\1\2', norm)
    slug = re.sub(r'\s+', '-', norm.strip())
    return slug.strip('-')


def _tokenize(text: str) -> set:
    tokens = set(_normalize(text).split())
    tokens -= IGNORE_TOKENS
    tokens -= {t for t in tokens if len(t) <= 1}
    return tokens


def _jaccard(a: set, b: set) -> float:
    u = a | b
    return len(a & b) / len(u) if u else 0.0


# ─── Playwright lazy init ─────────────────────────────────────────────────────

def _get_page():
    """Inicializa Playwright en el primer uso. Se cierra al final del proceso."""
    if _PW["page"] is not None:
        return _PW["page"]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright no instalado: pip install playwright && "
                  "playwright install chromium")
        return None

    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox",
              "--disable-dev-shm-usage", "--disable-gpu"],
    )
    ctx = browser.new_context(
        user_agent=USER_AGENT,
        locale="es-ES",
        viewport={"width": 1440, "height": 900},
        extra_http_headers={"Accept-Language": "es-ES,es;q=0.9,en;q=0.8"},
        ignore_https_errors=True,
    )
    page = ctx.new_page()
    _PW.update({"pw": pw, "browser": browser, "ctx": ctx, "page": page})

    def _cleanup():
        try:
            browser.close()
            pw.stop()
        except Exception:
            pass
    atexit.register(_cleanup)
    return page


# ─── Extracción de imágenes ──────────────────────────────────────────────────

def _extract_images(page) -> list:
    """
    Extrae URLs de imágenes en orden de aparición en la página.
    La imagen principal (og:image) siempre va primero; el resto en orden DOM.
    El filtrado de banners y la síntesis de URLs /wysiwyg/ se aplican al final
    vía _augment_with_wysiwyg().
    """
    raw_order: list = []
    seen_raw: set = set()

    def _add(url: str):
        if not url:
            return
        full = url if url.startswith("http") else (
            f"https:{url}" if url.startswith("//") else ""
        )
        if not full or BASE_DOMAIN not in full:
            return
        if full in seen_raw:
            return
        seen_raw.add(full)
        raw_order.append(full)

    # 1. og:image — imagen principal canónica del producto
    try:
        og = page.query_selector("meta[property='og:image']")
        if og:
            _add(og.get_attribute("content") or "")
        og_alt = page.query_selector("meta[property='og:image:secure_url']")
        if og_alt:
            _add(og_alt.get_attribute("content") or "")
    except Exception:
        pass

    # 2. JSON-LD (schema.org Product) — orden tal como aparece en el array
    try:
        ld_texts = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
                        .map(s => s.textContent || '');
        }""")
        for text in (ld_texts or []):
            try:
                data = json.loads(text)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    imgs = item.get("image", [])
                    if isinstance(imgs, str):
                        imgs = [imgs]
                    for img in imgs:
                        if isinstance(img, str):
                            _add(img)
                        elif isinstance(img, dict):
                            _add(img.get("url") or img.get("contentUrl") or "")
            except Exception:
                pass
    except Exception:
        pass

    # 3. <img> en orden DOM (galería del producto)
    try:
        for el in page.query_selector_all("img"):
            srcset = el.get_attribute("srcset") or ""
            parts  = [p.strip() for p in srcset.split(",") if p.strip()]
            if parts:
                cand = parts[-1].split()[0]
                if cand and not cand.startswith("data:"):
                    _add(cand)

            for attr in ("data-src", "data-lazy-src", "data-zoom-image",
                         "data-large-image", "data-full-url"):
                val = el.get_attribute(attr) or ""
                if val and not val.startswith("data:"):
                    _add(val)

            src = el.get_attribute("src") or ""
            if src and not src.startswith("data:"):
                _add(src)
    except Exception:
        pass

    return _augment_with_wysiwyg(raw_order)


def _try_url(page, url: str, title_tokens: set) -> tuple:
    """
    Visita la URL y devuelve (name, images) si es una página de producto válida.
    Si la página no existe, no es producto, o no comparte tokens con el título,
    devuelve (None, []).
    """
    try:
        resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
        if resp and resp.status >= 400:
            log.debug(f"  HTTP {resp.status} en {url}")
            return None, []
        page.wait_for_timeout(2000)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)

        name_el = (
            page.query_selector("h1.product_title")
            or page.query_selector("h1.product-title")
            or page.query_selector("h1.entry-title")
            or page.query_selector("h1")
        )
        name = name_el.inner_text().strip() if name_el else ""
        if not name:
            return None, []

        # Sanity check: el h1 debe compartir tokens con el título Shopify.
        # Para títulos con ≥3 tokens significativos exigimos ≥2 coincidencias
        # (evita falsos positivos tipo VIOLET → CLASSIC compartiendo solo "perfume").
        name_tokens = _tokenize(name)
        if title_tokens:
            overlap = title_tokens & name_tokens
            min_req = 2 if len(title_tokens) >= 3 else 1
            if len(overlap) < min_req:
                log.info(f"  Sanity check falla ({len(overlap)}/{min_req} tokens "
                         f"de {sorted(title_tokens)} ∩ {sorted(name_tokens)}): '{name}'")
                return None, []

        images = _extract_images(page)
        return name, images
    except Exception as e:
        log.debug(f"  _try_url error: {e}")
        return None, []


# ─── Búsqueda DDG con filtro site: ────────────────────────────────────────────

def _ddg_find_product_url(title: str) -> str | None:
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            log.warning("  [DDG] ddgs no instalado")
            return None

    clean = _clean_title(title)
    query = f"site:artero.com/es/petcare/ {clean}"
    log.info(f"  [DDG] {query}")
    for attempt in range(3):
        try:
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=10):
                    url = r.get("href") or r.get("url") or ""
                    if "artero.com/es/petcare/" not in url:
                        continue
                    url = url.split("?")[0].split("#")[0].rstrip("/")
                    # Excluir la propia categoría
                    if url.rstrip("/").endswith("/petcare"):
                        continue
                    log.info(f"  [DDG] → {url}")
                    return url
            break
        except Exception as e:
            wait = 3 * (2 ** attempt)
            if attempt < 2:
                log.warning(f"  [DDG] error ({attempt+1}/3): {e} — reintento en {wait}s")
                time.sleep(wait)
            else:
                log.warning(f"  [DDG] error final: {e}")
    return None


# ─── Persistencia del catálogo ────────────────────────────────────────────────

def _save_catalog(catalog: dict):
    try:
        CATALOG_PATH.parent.mkdir(exist_ok=True)
        CATALOG_PATH.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log.warning(f"No se pudo guardar catálogo: {e}")


# ─── Interfaz pública ─────────────────────────────────────────────────────────

_CACHE_RE  = re.compile(
    r'^(https?://[^/]+/media)/catalog/product/cache/[0-9a-f]+/'
    r'(?:[^/]+/[^/]+/)?([^?]+)$'
)
_BANNER_HEX_RE = re.compile(r'[0-9a-f]{40,}')

# Hash de Magento que genera las imágenes de galería a tamaño completo
# (~1100×1100) en artero.com. Las demás variantes de hash son thumbnails
# que pueden llegar a 110×110 o 265×265. Cuando el scraper solo encuentra
# una URL con hash desconocido (thumbnail), sintetizamos también la URL
# con este hash para intentar obtener la versión de alta resolución.
_ARTERO_FULLSIZE_HASH = "7c9c60b8f976989d414fc48458336f45"


def _should_keep_url(url: str) -> bool:
    """Filtra URLs no-producto (banners, logos, badges, video thumbs)."""
    low = url.lower()
    if any(kw in low for kw in (".svg", "logo", "icon", "banner",
                                 "sprite", "placeholder", "favicon",
                                 "_bnd", "/static/", "/.renditions/",
                                 "hqdefault", "maxresdefault", "sddefault")):
        return False
    filename = url.rstrip("/").split("/")[-1].rsplit(".", 1)[0].lower()
    if _BANNER_HEX_RE.search(filename):
        return False
    # Miniaturas de vídeo Vimeo: {id}-{hash}-d_{w}x{h}
    if re.search(r'-d_\d+x\d+$', filename):
        return False
    return True


def _augment_with_wysiwyg(urls: list) -> list:
    """
    Filtra URLs no-producto y, para cada /cache/ URL, añade inmediatamente
    después (preserva orden):
      1. La contraparte /wysiwyg/ (imagen original sin recortar)
      2. La URL de cache con el hash de tamaño completo de Artero
         (_ARTERO_FULLSIZE_HASH ≈ 1100×1100), para el caso en que solo
         tengamos la URL de un thumbnail y el wysiwyg no exista.
    Idempotente.
    """
    ordered: list = []
    seen: set = set()
    for u in urls:
        if not u or not _should_keep_url(u):
            continue
        clean = re.sub(r'-\d+x\d+\.', '.', u).split("?")[0]
        if clean in seen:
            continue
        seen.add(clean)
        ordered.append(clean)
        m = _CACHE_RE.match(clean)
        if m:
            base, filename = m.group(1), m.group(2)
            # 1. wysiwyg
            wysiwyg = f"{base}/wysiwyg/{filename}"
            if wysiwyg not in seen:
                seen.add(wysiwyg)
                ordered.append(wysiwyg)
            # 2. Cache full-size (si el filename tiene ≥2 chars para los subdirs)
            if len(filename) >= 2:
                f1, f2 = filename[0].lower(), filename[1].lower()
                full_cache = (f"{base}/catalog/product/cache/"
                              f"{_ARTERO_FULLSIZE_HASH}/{f1}/{f2}/{filename}")
                if full_cache not in seen:
                    seen.add(full_cache)
                    ordered.append(full_cache)
    return ordered


def scrape_catalog(web_url: str, rebuild: bool = False) -> dict:
    """
    Carga el catálogo cacheado. NO realiza scraping bulk del listado.
    El catálogo se rellena bajo demanda en find_best_match().
    Con rebuild=True se borra el caché para forzar nueva resolución de cada producto.

    Al cargar, rehidrata las entradas existentes: filtra banners y añade las
    contrapartes /wysiwyg/ que las versiones antiguas del scraper no incluían.
    """
    if rebuild and CATALOG_PATH.exists():
        CATALOG_PATH.unlink()
        log.info("Catálogo borrado (rebuild) — se reconstruirá bajo demanda")
        return {}
    if CATALOG_PATH.exists():
        try:
            catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
            for entry in catalog.values():
                entry["images"] = _augment_with_wysiwyg(entry.get("images", []))
            log.info(f"Catálogo cargado desde caché: {len(catalog)} productos "
                     f"(URLs rehidratadas con /wysiwyg/)")
            return catalog
        except Exception:
            pass
    log.info("Catálogo vacío — se rellenará bajo demanda")
    return {}


def _slug_candidates(title: str) -> list[str]:
    """
    Genera la lista de slugs a probar antes de DDG, en orden de prioridad:
      1. Corrección manual de _SLUG_CORRECTIONS si existe
      2. Slug derivado del título
      3. Variante sin 'higiene' (la web omite este prefijo de categoría)
    """
    primary = _title_to_slug(title)
    candidates: list[str] = []
    if primary in _SLUG_CORRECTIONS:
        candidates.append(_SLUG_CORRECTIONS[primary])
    candidates.append(primary)
    if "higiene" in primary.split("-"):
        without = "-".join(t for t in primary.split("-") if t != "higiene")
        if without and without not in candidates:
            candidates.append(without)
    # Quitar duplicados preservando orden
    seen, ordered = set(), []
    for s in candidates:
        if s and s not in seen:
            seen.add(s); ordered.append(s)
    return ordered


def find_best_match(shopify_title: str, catalog: dict) -> tuple:
    """
    Resolución por producto sin Jaccard contra el catálogo entero
    (causaba falsos positivos: 'CHAMPU DETOX' matcheaba 'CHAMPU BLANC' por
    compartir 'champu'). Estrategia:

      1. Buscar cada slug candidato en el catálogo (cache hit)
      2. Probar URL directa para cada candidato
      3. DDG con site:artero.com/es/petcare/
      4. Cachear bajo todos los alias para futuras ejecuciones
    """
    title_tokens = _tokenize(shopify_title)
    expected_slug = _title_to_slug(shopify_title)
    candidates = _slug_candidates(shopify_title)

    # 1. Cache exacto en cualquier candidato
    for slug in candidates:
        if slug in catalog:
            log.info(f"  Match caché exacto: {slug}")
            return slug, 1.0

    page = _get_page()
    if page is None:
        return None, 0.0

    # 2. Slug directo para cada candidato
    for slug in candidates:
        direct_url = f"{BASE_URL}/{slug}"
        log.info(f"  [slug] {direct_url}")
        name, images = _try_url(page, direct_url, title_tokens)
        if name:
            entry = {"name": name, "url": direct_url, "images": images}
            for alias in candidates:
                catalog[alias] = entry
            _save_catalog(catalog)
            log.info(f"  ✓ Resuelto vía slug directo: {slug} ({len(images)} imgs)")
            return slug, 1.0

    # 3. Fallback DDG
    ddg_url = _ddg_find_product_url(shopify_title)
    if ddg_url:
        name, images = _try_url(page, ddg_url, title_tokens)
        if name:
            ddg_slug = ddg_url.rstrip("/").split("/")[-1]
            entry = {"name": name, "url": ddg_url, "images": images}
            catalog[ddg_slug] = entry
            for alias in candidates:
                if alias != ddg_slug:
                    catalog[alias] = entry
            _save_catalog(catalog)
            log.info(f"  ✓ Resuelto vía DDG: {ddg_slug} ({len(images)} imgs)")
            return ddg_slug, 1.0

    log.warning(f"  Sin resolución para slug esperado '{expected_slug}'")
    return None, 0.0
