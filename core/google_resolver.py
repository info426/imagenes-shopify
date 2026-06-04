"""
Resolver genérico basado en Google (Custom Search API) — replica el método manual:
  1. Copiar el NOMBRE completo del producto Shopify.
  2. Buscarlo en Google restringido al dominio del fabricante (`{nombre} site:dominio`).
     La web oficial posiciona su ficha de producto en los primeros resultados (SEO).
  3. Entrar a los primeros resultados y CONFIRMAR por imagen (foto Shopify ↔ foto de
     la página) y por nombre (doble verificación).
  4. El primer resultado (orden de Google) que confirma por imagen y nombre gana.
     Si ninguno confirma → vacío (no se inventa la URL).

Es brand-agnóstico: solo necesita el dominio (derivado de web_url). No requiere un
scraper a medida por marca. Requiere GOOGLE_API_KEY + GOOGLE_CSE_ID (motor CSE con
"buscar en toda la web" activado). 100 búsquedas/día gratis.
"""

import atexit
import json
import logging
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from urllib.parse import urljoin, urlparse

try:
    from core import image_match
except Exception:  # pragma: no cover
    image_match = None

log = logging.getLogger(__name__)

# Nº de resultados de Google que se visitan y confirman por producto.
TOPK = int(os.getenv("GOOGLE_TOPK", "4"))
# Overlap mínimo de nombre (Jaccard) para la doble verificación.
NAME_MIN_WITH_IMG = 0.10   # con gate de imagen, el nombre solo descarta lo absurdo
NAME_MIN_TEXT     = 0.22   # sin imagen, exige más coincidencia de nombre

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_HEADED = (os.getenv("APPLAWS_HEADED") or os.getenv("GOOGLE_HEADED") or "") \
    in ("1", "true", "True")

_PW = {"pw": None, "browser": None, "ctx": None, "page": None, "warm": ""}

# Stopwords + ruido de tienda que no ayudan a verificar el nombre
_STOP = {
    "de", "la", "el", "los", "las", "con", "sin", "y", "e", "o", "a", "para",
    "un", "una", "al", "the", "and", "with", "for", "of",
    "kg", "gr", "g", "mg", "ml", "l", "lb", "cm", "mm", "x", "ud", "uds",
    "pack", "caja", "bolsa", "ndr", "pv", "nv", "online",
}
# Sinónimos ES↔EN frecuentes en alimentación de mascotas (verificación de nombre)
_SYN = {
    "perro": {"dog"}, "dog": {"perro"}, "gato": {"cat"}, "cat": {"gato"},
    "cachorro": {"puppy"}, "puppy": {"cachorro"}, "gatito": {"kitten"},
    "kitten": {"gatito"}, "adulto": {"adult"}, "adult": {"adulto"},
    "pollo": {"chicken"}, "chicken": {"pollo"}, "cordero": {"lamb"},
    "lamb": {"cordero"}, "salmon": {"salmon"}, "atun": {"tuna"}, "tuna": {"atun"},
    "ternera": {"beef"}, "vacuno": {"beef"}, "beef": {"ternera", "vacuno"},
    "pavo": {"turkey"}, "turkey": {"pavo"}, "pato": {"duck"}, "duck": {"pato"},
    "arroz": {"rice"}, "rice": {"arroz"}, "pescado": {"fish"}, "fish": {"pescado"},
    "lata": {"can", "wet"}, "humedo": {"wet"}, "esterilizado": {"sterilised", "neutered"},
}

# Especie (perro ≠ gato) — guard determinista de la doble verificación
_DOG = {"dog", "dogs", "perro", "perros", "canine", "canino"}
_CAT = {"cat", "cats", "gato", "gatos", "feline", "felino", "kitten", "gatito"}


# ─── Texto ────────────────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    text = unicodedata.normalize("NFD", (text or "").lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9\s]", " ", text)


def _tokens(text: str) -> set:
    toks = set(_norm(text).split()) - _STOP
    return {t for t in toks if len(t) > 1}


def _expand(toks: set) -> set:
    out = set(toks)
    for t in toks:
        out |= _SYN.get(t, set())
    return out


def _name_sim(a: str, b: str) -> float:
    ta, tb = _expand(_tokens(a)), _expand(_tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _species(text: str) -> str:
    toks = set(_norm(text).split())
    d, c = bool(toks & _DOG), bool(toks & _CAT)
    return "dog" if (d and not c) else ("cat" if (c and not d) else "")


def _species_ok(title_sp: str, cand_text: str) -> bool:
    cs = _species(cand_text)
    return not (title_sp and cs and title_sp != cs)


def _clean_title(title: str) -> str:
    """Quita marcadores de tienda (*...*, (NDR)) — busca como lo haría una persona."""
    t = re.sub(r"\*[^*]*\*|\((?:NDR|PV|NV|ONLINE)\)", " ", title, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", t).strip()


# ─── Dominio ──────────────────────────────────────────────────────────────────

def domain_of(web_url: str) -> str:
    host = urlparse(web_url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


# ─── Google Custom Search (web) ───────────────────────────────────────────────

def _cse_web_search(query: str, num: int = 10) -> list:
    """Búsqueda WEB en Google CSE → [{'url','title','snippet'}] en orden de Google."""
    key = os.getenv("GOOGLE_API_KEY", "")
    cx = os.getenv("GOOGLE_CSE_ID", "")
    if not key or not cx:
        log.warning("  [google] faltan GOOGLE_API_KEY / GOOGLE_CSE_ID")
        return []
    params = urllib.parse.urlencode({
        "key": key, "cx": cx, "q": query,
        "num": min(num, 10), "hl": "es", "gl": "es",
    })
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                f"https://www.googleapis.com/customsearch/v1?{params}",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
            out = []
            for it in data.get("items", []):
                out.append({"url": it.get("link", ""),
                            "title": it.get("title", ""),
                            "snippet": it.get("snippet", "")})
            return out
        except Exception as e:
            wait = 3 * (2 ** attempt)
            log.warning(f"  [google] error ({attempt+1}/3): {e}"
                        + (f" — reintento en {wait}s" if attempt < 2 else ""))
            if attempt < 2:
                time.sleep(wait)
    return []


def candidate_urls(title: str, domain: str, max_urls: int = TOPK) -> list:
    """URLs candidatas en el dominio del fabricante, en orden de relevancia Google."""
    clean = _clean_title(title)
    seen, urls = set(), []

    def _collect(items):
        for it in items:
            u = (it.get("url") or "").split("#")[0]
            if not u or domain not in urlparse(u).netloc.lower():
                continue
            if u in seen:
                continue
            seen.add(u)
            urls.append(u)
            if len(urls) >= max_urls:
                return

    # 1) nombre + site:dominio (lo más directo)
    log.info(f"  [google] «{clean} site:{domain}»")
    _collect(_cse_web_search(f"{clean} site:{domain}", num=10))
    # 2) fallback: búsqueda libre filtrando al dominio (como el usuario, sin site:)
    if len(urls) < 2:
        log.info(f"  [google] (libre) «{clean}»")
        _collect(_cse_web_search(clean, num=10))
    log.info(f"  [google] {len(urls)} candidatos: {urls}")
    return urls


# ─── Playwright (genérico, anti-bot) ──────────────────────────────────────────

def _get_page(domain: str):
    if _PW["page"] is not None:
        return _PW["page"]
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
    except ImportError:
        log.error("Playwright no instalado")
        return None
    browser = pw.chromium.launch(
        headless=not _HEADED,
        args=["--no-sandbox", "--disable-setuid-sandbox",
              "--disable-dev-shm-usage", "--disable-gpu"],
    )
    ctx = browser.new_context(
        user_agent=USER_AGENT, locale="es-ES",
        viewport={"width": 1440, "height": 900},
        extra_http_headers={
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,*/*;q=0.8",
            "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        },
        ignore_https_errors=True,
    )
    ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        "Object.defineProperty(navigator,'languages',{get:()=>['es-ES','es','en']});"
        "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
        "window.chrome={runtime:{}};"
    )
    page = ctx.new_page()
    _PW.update({"pw": pw, "browser": browser, "ctx": ctx, "page": page})
    atexit.register(lambda: browser.close() if browser else None)
    return page


def _warm_up(page, domain: str):
    if _PW.get("warm") == domain:
        return
    try:
        page.goto(f"https://www.{domain}/", timeout=20000, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        _PW["warm"] = domain
    except Exception as e:
        log.debug(f"  [warm-up] {domain}: {e}")


# ─── Extracción de nombre + imagen del candidato ──────────────────────────────

def _extract_images(page, page_url: str) -> list:
    ordered, seen = [], set()

    def _add(u):
        if not u or u.startswith("data:"):
            return
        full = urljoin(page_url, u)
        if not full.startswith("http"):
            return
        low = full.lower()
        if any(k in low for k in (".svg", "logo", "icon", "sprite", "placeholder",
                                  "favicon", "loader", "spinner", "pixel", "banner")):
            return
        clean = full.split("?")[0]
        if clean in seen:
            return
        seen.add(clean)
        ordered.append(full)

    try:
        for sel in ("meta[property='og:image']",
                    "meta[property='og:image:secure_url']",
                    "meta[name='twitter:image']"):
            el = page.query_selector(sel)
            if el:
                _add(el.get_attribute("content") or "")
    except Exception:
        pass
    try:
        for txt in page.evaluate(
            "() => Array.from(document.querySelectorAll('script[type=\"application/ld+json\"]'))"
            ".map(s => s.textContent || '')") or []:
            try:
                data = json.loads(txt)
                items = data if isinstance(data, list) else (
                    data.get("@graph", [data]) if isinstance(data, dict) else [])
                for it in items:
                    if isinstance(it, dict):
                        im = it.get("image")
                        if isinstance(im, str):
                            _add(im)
                        elif isinstance(im, list):
                            for x in im:
                                _add(x if isinstance(x, str) else (x or {}).get("url", ""))
                        elif isinstance(im, dict):
                            _add(im.get("url") or im.get("contentUrl") or "")
            except Exception:
                pass
    except Exception:
        pass
    return ordered


def _try_url(page, url: str) -> tuple:
    """Visita la URL y devuelve (nombre, [imágenes]). (None, []) si falla."""
    try:
        resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
        if resp and resp.status >= 400:
            log.info(f"    HTTP {resp.status} {url}")
            return None, []
        try:
            page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        page.wait_for_timeout(800)
        name = ""
        for sel in ("meta[property='og:title']", "h1"):
            el = page.query_selector(sel)
            if el:
                name = (el.get_attribute("content") if sel.startswith("meta")
                        else el.inner_text()) or ""
                name = name.strip()
                if name:
                    break
        return (name or ""), _extract_images(page, page.url)
    except Exception as e:
        log.debug(f"    _try_url {url}: {e}")
        return None, []


def _fetch_bytes(page, url: str):
    try:
        r = page.context.request.get(url, timeout=30000)
        if r.ok:
            return r.body()
    except Exception:
        pass
    return None


# ─── Resolver ─────────────────────────────────────────────────────────────────

def resolve(title: str, product_images: list, domain: str) -> tuple:
    """
    Devuelve (url, score) de la ficha oficial confirmada, o (None, 0.0) si nada
    confirma. score = overlap de nombre del candidato elegido.
    """
    if not domain:
        return None, 0.0
    urls = candidate_urls(title, domain)
    if not urls:
        log.warning(f"  Sin resultados Google para '{title}'")
        return None, 0.0

    page = _get_page(domain)
    if page is None:
        return None, 0.0
    _warm_up(page, domain)

    # Huellas visuales de las fotos Shopify (CDN público)
    q_feats = []
    if image_match is not None and getattr(image_match, "ENABLED", False):
        for u in (product_images or [])[:3]:
            f = image_match.compute_feature_from_url(u)
            if f:
                q_feats.append(f)
    gate_active = bool(q_feats) and image_match is not None \
        and image_match.backend() == "clip"
    gate = image_match.gate_threshold() if gate_active else 0.0
    title_sp = _species(title)
    name_min = NAME_MIN_WITH_IMG if gate_active else NAME_MIN_TEXT

    # Recorre los candidatos EN ORDEN DE GOOGLE; el primero que confirma gana.
    best_reject = None
    for rank, url in enumerate(urls, 1):
        name, images = _try_url(page, url)
        if not name:
            continue
        nsim = _name_sim(title, name)
        # Doble verificación de nombre (especie + overlap mínimo)
        if not _species_ok(title_sp, name + " " + url):
            log.info(f"  [#{rank}] '{name[:50]}' nsim={nsim:.2f} — especie ≠ → descartado")
            continue
        if nsim < name_min:
            log.info(f"  [#{rank}] '{name[:50]}' nsim={nsim:.2f} < {name_min} → descartado")
            continue
        # Confirmación por imagen
        if gate_active:
            cf = None
            for iu in images[:2]:
                data = _fetch_bytes(page, iu)
                cf = image_match.compute_feature(data) if data else None
                if cf:
                    break
            isim = image_match.best_similarity(q_feats, cf) if cf else 0.0
            if isim < gate:
                log.info(f"  [#{rank}] '{name[:50]}' nsim={nsim:.2f} img={isim:.2f} "
                         f"< {gate:.2f} → no confirma")
                best_reject = best_reject or (url, nsim)
                continue
            log.info(f"  ✓ [#{rank}] CONFIRMADO '{name[:50]}' nsim={nsim:.2f} img={isim:.2f}")
            return url, round(nsim, 3)
        else:
            # Sin imagen: confiamos en el ranking de Google + overlap de nombre
            log.info(f"  ✓ [#{rank}] (sin imagen) '{name[:50]}' nsim={nsim:.2f}")
            return url, round(nsim, 3)

    log.warning(f"  Ningún candidato confirma para '{title}' → vacío")
    return None, 0.0
