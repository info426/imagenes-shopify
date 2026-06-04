"""
Fuente Distrivet (proveedor) — imágenes por búsqueda de EAN.

Distrivet es el proveedor de shopypet.eu, así que tiene la ficha (con foto) de
TODOS los productos de la tienda. Como cada producto Shopify ya lleva su EAN en
`variant.barcode`, conseguir la imagen es un LOOKUP EXACTO por EAN — sin matching
difuso, sin DDG, sin falsos positivos — y BRAND-AGNÓSTICO (un solo módulo para
todas las marcas).

La web (`tienda.distrivet.es`, ASP clásico) devuelve HTTP 403 a peticiones sin
navegador y requiere login, así que toda la navegación se hace con Playwright +
plantilla anti-bot (Chrome UA, Sec-Fetch-*, bypass navigator.webdriver, warm-up).

Flujo (lo orquesta core/process_brand.py → run_distrivet):
  login(user, pwd)                  → inicia sesión UNA vez (sesión reutilizada)
  images_for_ean(ean) -> list[str]  → busca el EAN, abre la ficha, extrae imágenes
  close()                           → cierra el navegador

Credenciales: NUNCA en el repo. Vienen de GitHub Secrets DISTRIVET_USER /
DISTRIVET_PASS (las lee el orquestador y las pasa a login()).

⚠️ Selectores: como la web da 403 sin sesión real, los nombres exactos del
formulario de login y del buscador NO se conocen de antemano. Este módulo los
resuelve con heurísticas y, sobre todo, VUELCA LA ESTRUCTURA del DOM en el log
(prefijo "[distrivet][diag]") la primera vez, para fijar selectores tras el 1er
test. Se pueden forzar selectores por variable de entorno (ver _ENV_* abajo) sin
tocar código.
"""

import atexit
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

log = logging.getLogger(__name__)

BASE_URL  = "https://tienda.distrivet.es/"
HOME_URL  = os.getenv("DISTRIVET_LOGIN_URL",
                      "https://tienda.distrivet.es/home-webshop.asp")
# Home del webshop (con buscador) a la que volvemos para buscar por EAN.
SHOP_URL  = os.getenv("DISTRIVET_SHOP_URL",
                      "https://tienda.distrivet.es/home-webshop.asp")
# Tras el login, Distrivet muestra una página de selección de dirección de
# entrega antes de dejar entrar al webshop (p. ej. bienvenido.asp). Hay que
# elegir una dirección para continuar.
ADDRESS_PAGE_PART = os.getenv("DISTRIVET_ADDRESS_PAGE", "bienvenido.asp").lower()
# Patrón de URL de la ficha de producto: webshop-producto.asp?NoProducto={ref}
PRODUCT_URL_PART  = os.getenv("DISTRIVET_PRODUCT_PART", "webshop-producto.asp").lower()
CACHE_PATH = Path("resultados/distrivet_ean_cache.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Overrides opcionales de selectores (CSS) — útiles para fijarlos tras el 1er
# test sin tocar código. Si están vacíos, se usan las heurísticas.
_ENV_USER_SEL   = os.getenv("DISTRIVET_USER_SEL", "")
_ENV_PASS_SEL   = os.getenv("DISTRIVET_PASS_SEL", "")
_ENV_SUBMIT_SEL = os.getenv("DISTRIVET_SUBMIT_SEL", "")
_ENV_SEARCH_SEL = os.getenv("DISTRIVET_SEARCH_SEL", "")
_ENV_SEARCH_SUBMIT_SEL = os.getenv("DISTRIVET_SEARCH_SUBMIT_SEL", "")
_ENV_PRODUCT_LINK_SEL  = os.getenv("DISTRIVET_PRODUCT_LINK_SEL", "")
# Selección de dirección de entrega (interstitial post-login)
_ENV_ADDRESS_SEL        = os.getenv("DISTRIVET_ADDRESS_SEL", "")
_ENV_ADDRESS_SUBMIT_SEL = os.getenv("DISTRIVET_ADDRESS_SUBMIT_SEL", "")

# Estado Playwright reutilizado en todo el proceso
_PW = {"pw": None, "browser": None, "ctx": None, "page": None}
_LOGGED_IN = False
_CACHE = None
_DIAG_DUMPED = {"login": False, "address": False, "search": False, "product": False}


# ─── Caché por EAN ──────────────────────────────────────────────────────────

def _load_cache() -> dict:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    if CACHE_PATH.exists():
        try:
            _CACHE = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            log.info(f"[distrivet] caché cargada: {len(_CACHE)} EANs")
            return _CACHE
        except Exception:
            pass
    _CACHE = {}
    return _CACHE


def save_cache():
    try:
        CACHE_PATH.parent.mkdir(exist_ok=True)
        CACHE_PATH.write_text(json.dumps(_load_cache(), ensure_ascii=False,
                                         indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"[distrivet] no se pudo guardar caché: {e}")


# ─── Playwright (anti-bot) ──────────────────────────────────────────────────

def _get_page():
    """Inicializa Playwright (anti-bot) en el primer uso."""
    if _PW["page"] is not None:
        return _PW["page"]
    try:
        from core.playwright_shared import get_playwright
        pw = get_playwright()
    except Exception:
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
        except ImportError:
            log.error("Playwright no instalado: pip install playwright && "
                      "playwright install chromium")
            return None

    browser = pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox",
              "--disable-dev-shm-usage", "--disable-gpu"],
    )
    ctx = browser.new_context(
        user_agent=USER_AGENT,
        locale="es-ES",
        viewport={"width": 1440, "height": 900},
        extra_http_headers={
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        },
        ignore_https_errors=True,
    )
    ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['es-ES', 'es', 'en']});
        window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
    """)
    page = ctx.new_page()
    _PW.update({"pw": pw, "browser": browser, "ctx": ctx, "page": page})

    def _cleanup():
        try:
            browser.close()
        except Exception:
            pass
    atexit.register(_cleanup)
    return page


def close():
    try:
        if _PW["browser"]:
            _PW["browser"].close()
    except Exception:
        pass
    _PW.update({"browser": None, "ctx": None, "page": None})


# ─── Diagnóstico: volcar la estructura del DOM ───────────────────────────────

def _dump_structure(page, label: str):
    """Vuelca formularios, inputs y enlaces de la página al log (una vez por
    `label`). Imprescindible para fijar selectores tras el 1er test, ya que la
    web no es inspeccionable sin sesión real."""
    if _DIAG_DUMPED.get(label):
        return
    _DIAG_DUMPED[label] = True
    pre = f"[distrivet][diag][{label}]"
    try:
        log.info(f"{pre} URL={page.url}")
        log.info(f"{pre} title={page.title()!r}")
    except Exception:
        pass
    # Formularios + sus inputs
    try:
        forms = page.evaluate("""() => Array.from(document.querySelectorAll('form')).map(f => ({
            action: f.getAttribute('action') || '',
            method: (f.getAttribute('method') || 'get').toLowerCase(),
            id: f.id || '', name: f.getAttribute('name') || '',
            inputs: Array.from(f.querySelectorAll('input,select,textarea,button')).map(i => ({
                tag: i.tagName.toLowerCase(),
                type: (i.getAttribute('type') || '').toLowerCase(),
                name: i.getAttribute('name') || '', id: i.id || '',
                placeholder: i.getAttribute('placeholder') || ''
            }))
        }))""")
    except Exception as e:
        log.info(f"{pre} no pude enumerar forms: {e}")
        forms = []
    for fi, f in enumerate(forms or []):
        log.info(f"{pre} form[{fi}] action={f.get('action')!r} "
                 f"method={f.get('method')} id={f.get('id')!r} name={f.get('name')!r}")
        for inp in f.get("inputs", []):
            log.info(f"{pre}   <{inp.get('tag')}> type={inp.get('type')!r} "
                     f"name={inp.get('name')!r} id={inp.get('id')!r} "
                     f"ph={inp.get('placeholder')!r}")
    # Inputs sueltos fuera de form
    try:
        loose = page.evaluate("""() => Array.from(document.querySelectorAll('input'))
            .filter(i => !i.closest('form'))
            .map(i => ({type:(i.getAttribute('type')||'').toLowerCase(),
                        name:i.getAttribute('name')||'', id:i.id||'',
                        placeholder:i.getAttribute('placeholder')||''}))""")
        for inp in (loose or []):
            log.info(f"{pre} input(suelto) type={inp.get('type')!r} "
                     f"name={inp.get('name')!r} id={inp.get('id')!r} "
                     f"ph={inp.get('placeholder')!r}")
    except Exception:
        pass


def _dump_links(page, label: str, limit: int = 40):
    """Vuelca los primeros enlaces (href + texto) — para identificar el patrón
    de URL de ficha de producto en la página de resultados."""
    pre = f"[distrivet][diag][{label}]"
    try:
        links = page.evaluate("""() => Array.from(document.querySelectorAll('a[href]'))
            .map(a => ({href: a.getAttribute('href') || '',
                        text: (a.innerText || '').trim().slice(0, 60)}))""")
    except Exception as e:
        log.info(f"{pre} no pude enumerar enlaces: {e}")
        return
    shown = 0
    for a in (links or []):
        href = a.get("href", "")
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        log.info(f"{pre} a href={href!r} text={a.get('text')!r}")
        shown += 1
        if shown >= limit:
            break


# ─── Login ──────────────────────────────────────────────────────────────────

def _password_field(page):
    if _ENV_PASS_SEL:
        return page.query_selector(_ENV_PASS_SEL)
    return page.query_selector("input[type='password']")


def _looks_logged_in(page) -> bool:
    """Heurística: tras login, suele desaparecer el campo password y aparecen
    enlaces de 'cerrar sesión'/'mi cuenta'."""
    try:
        if _password_field(page) is not None:
            # Aún hay campo password → probablemente NO logueado
            pass
        body = (page.inner_text("body") or "").lower()
    except Exception:
        body = ""
    indicators = ("cerrar sesión", "cerrar sesion", "desconectar", "salir",
                  "mi cuenta", "mis pedidos", "cerrar la sesión", "logout")
    return any(ind in body for ind in indicators)


def _on_address_page(page) -> bool:
    """¿Estamos en el interstitial de selección de dirección de entrega?"""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if ADDRESS_PAGE_PART and ADDRESS_PAGE_PART in url:
        return True
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        body = ""
    return ("direccion de entrega" in body or "dirección de entrega" in body
            or ("seleccione" in body and "direccion" in body)
            or ("seleccione" in body and "dirección" in body))


def _select_delivery_address(page) -> bool:
    """Pasa el interstitial post-login de selección de dirección de entrega
    (Distrivet → bienvenido.asp): marca un radio de dirección (`customer_web`) y
    pulsa «Seleccionar dirección».

    Robusto ante la navegación que dispara el envío: cualquier llamada al DOM
    puede fallar con 'Execution context was destroyed' justo cuando la página
    navega — se captura y se interpreta como «ya no estamos en la dirección»
    (éxito). La función NUNCA lanza. Vuelca [distrivet][diag][address] para fijar
    el botón con DISTRIVET_ADDRESS_SUBMIT_SEL si hiciera falta."""
    try:
        if not _on_address_page(page):
            return False
    except Exception:
        return False
    try:
        log.info(f"[distrivet] selección de dirección de entrega detectada "
                 f"(URL={page.url})")
    except Exception:
        pass
    _dump_structure(page, "address")
    _dump_links(page, "address")

    def _qs(selector):
        try:
            return page.query_selector(selector)
        except Exception:
            return None

    def _settle():
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        try:
            page.wait_for_timeout(1000)
        except Exception:
            pass

    def _still_here():
        # Si el contexto se destruyó por navegación → ya salimos de la dirección.
        try:
            return _on_address_page(page)
        except Exception:
            return False

    # 1. Marcar un radio de dirección (customer_web; el primero o DISTRIVET_ADDRESS_ID)
    addr_id = os.getenv("DISTRIVET_ADDRESS_ID", "")
    target = None
    if addr_id:
        target = (_qs(f"input[type='radio'][id='{addr_id}']")
                  or _qs(f"input[type='radio'][value='{addr_id}']"))
    if target is None:
        target = (_qs("input[type='radio'][name='customer_web']")
                  or _qs("input[type='radio']"))
    if target is not None:
        try:
            target.check(timeout=4000)
        except Exception:
            try:
                target.click(timeout=4000)
            except Exception:
                pass
        log.info("[distrivet] radio de dirección marcado")
        _settle()

    # 2. Pulsar «Seleccionar dirección» (botón principal de bienvenido.asp)
    if _still_here():
        candidates = []
        if _ENV_ADDRESS_SUBMIT_SEL:
            candidates.append(_ENV_ADDRESS_SUBMIT_SEL)
        candidates += [
            "button:has-text('Seleccionar dirección')",
            "button:has-text('Seleccionar direcci')",
            "input[value*='Seleccionar direcci' i]",
            "a:has-text('Seleccionar dirección')",
            "text=Seleccionar dirección",
            "button:has-text('Seleccionar')",
            "input[value*='seleccionar' i]",
            "input[value*='continuar' i]", "input[value*='aceptar' i]",
            "input[type='submit']", "button[type='submit']",
        ]
        for sel in candidates:
            el = _qs(sel)
            if not el:
                continue
            try:
                el.click(timeout=5000)
                log.info(f"[distrivet] botón de dirección pulsado: {sel}")
            except Exception:
                # La propia navegación puede destruir el contexto → se da por OK
                log.info(f"[distrivet] botón de dirección pulsado "
                         f"(navegación en curso): {sel}")
            _settle()
            if not _still_here():
                break

    # 3. Último recurso: enviar por JS (click del primer botón / submit del form)
    if _still_here():
        try:
            page.evaluate("""() => {
                const b = document.querySelector("button, input[type=submit]");
                if (b) { b.click(); return; }
                const f = document.querySelector('form'); if (f) f.submit();
            }""")
            _settle()
        except Exception:
            pass

    handled = not _still_here()
    if handled:
        try:
            log.info(f"[distrivet] ✓ dirección de entrega resuelta (URL={page.url})")
        except Exception:
            log.info("[distrivet] ✓ dirección de entrega resuelta")
    else:
        log.warning("[distrivet] no pude pasar la selección de dirección. Revisa "
                    "[distrivet][diag][address] y fija DISTRIVET_ADDRESS_SUBMIT_SEL.")
    return handled


def login(user: str, pwd: str) -> bool:
    """Inicia sesión en Distrivet (una vez). Devuelve True si parece logueado."""
    global _LOGGED_IN
    if _LOGGED_IN:
        return True
    page = _get_page()
    if page is None:
        return False

    # Warm-up + carga de la página de login
    try:
        resp = page.goto(HOME_URL, timeout=40000, wait_until="domcontentloaded")
        status = resp.status if resp else "?"
        log.info(f"[distrivet] login GET {HOME_URL} → HTTP {status}")
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
    except Exception as e:
        log.error(f"[distrivet] no pude cargar la página de login: {e}")
        return False

    _dump_structure(page, "login")

    # Localizar campos de usuario y contraseña
    pass_el = _password_field(page)
    if _ENV_USER_SEL:
        user_el = page.query_selector(_ENV_USER_SEL)
    else:
        user_el = None
        for sel in ("input[name*='usuario' i]", "input[id*='usuario' i]",
                    "input[name*='user' i]", "input[id*='user' i]",
                    "input[name*='login' i]", "input[id*='login' i]",
                    "input[name*='email' i]", "input[type='email']",
                    "input[name*='nif' i]", "input[name*='cliente' i]",
                    "input[name*='codigo' i]"):
            user_el = page.query_selector(sel)
            if user_el:
                break
        # Último recurso: el primer text input que precede al password
        if user_el is None:
            texts = page.query_selector_all(
                "input[type='text'], input:not([type])")
            if texts:
                user_el = texts[0]

    if not user_el or not pass_el:
        log.error("[distrivet] no encontré los campos de login. Revisa el "
                  "volcado [distrivet][diag][login] y fija DISTRIVET_USER_SEL / "
                  "DISTRIVET_PASS_SEL.")
        return False

    try:
        user_el.fill(user)
        pass_el.fill(pwd)
        log.info("[distrivet] credenciales introducidas — enviando formulario")
    except Exception as e:
        log.error(f"[distrivet] no pude rellenar el login: {e}")
        return False

    # Enviar: botón submit explícito, o Enter en el campo password
    submitted = False
    if _ENV_SUBMIT_SEL:
        btn = page.query_selector(_ENV_SUBMIT_SEL)
        if btn:
            try:
                btn.click()
                submitted = True
            except Exception:
                pass
    if not submitted:
        for sel in ("input[type='submit']", "button[type='submit']",
                    "button[name*='login' i]", "input[name*='login' i]",
                    "input[value*='entrar' i]", "input[value*='acceder' i]",
                    "button:has-text('Entrar')", "button:has-text('Acceder')"):
            btn = page.query_selector(sel)
            if btn:
                try:
                    btn.click()
                    submitted = True
                    break
                except Exception:
                    continue
    if not submitted:
        try:
            pass_el.press("Enter")
            submitted = True
        except Exception as e:
            log.error(f"[distrivet] no pude enviar el formulario: {e}")
            return False

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(1500)

    # Interstitial post-login: selección de dirección de entrega (bienvenido.asp)
    try:
        _select_delivery_address(page)
    except Exception as e:
        log.warning(f"[distrivet] error en selección de dirección (continúo): {e}")

    # Asegurar que estamos en una página con buscador (home del webshop)
    if _find_search_input(page) is None:
        try:
            page.goto(SHOP_URL, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
        except Exception:
            pass

    has_search = _find_search_input(page) is not None
    confirmed = _looks_logged_in(page) or has_search
    _LOGGED_IN = True  # seguimos; la búsqueda confirma/desmiente definitivamente
    if confirmed:
        log.info(f"[distrivet] ✓ sesión lista (URL={page.url}, "
                 f"buscador={'sí' if has_search else 'no'})")
    else:
        log.warning(f"[distrivet] login enviado pero no confirmo sesión por "
                    f"heurística (URL={page.url}, sin buscador detectado). "
                    f"Continúo; revisa el volcado de diagnóstico si las búsquedas "
                    f"fallan.")
    return True


# ─── Filtrado / upgrade de URLs de imagen ────────────────────────────────────

def _should_keep_url(url: str) -> bool:
    low = url.lower()
    if any(kw in low for kw in (".svg", "logo", "icon", "banner", "sprite",
                                "placeholder", "favicon", "/static/", "loader",
                                "spinner", "flag", "pixel", "blank", "noimage",
                                "sin-imagen", "sinimagen", "nofoto", "carrito",
                                "boton", "btn-")):
        return False
    return True


def _upgrade_url(url: str) -> str:
    """Intenta apuntar a la versión de mayor resolución de la imagen.
    - Quita el sufijo -WxH (WordPress) si lo hubiera.
    - Quita marcadores comunes de thumbnail en ASP/B2B (_p, _peq, _thumb, _s)."""
    clean = re.sub(r'-\d+x\d+(\.[a-zA-Z]{3,4})(?:\?.*)?$', r'\1', url.split("?")[0])
    return clean


# ─── Extracción de imágenes de la ficha ──────────────────────────────────────

def _extract_images(page, page_url: str, ean: str = "") -> list:
    """Extrae la foto del PRODUCTO de la ficha Distrivet.

    Las imágenes del CDN de Distrivet se nombran por EAN (p. ej. 064992280185.webp),
    igual que beaphar. La ficha incluye un carrusel "También le puede interesar"
    (relacionados, con OTROS EAN) → la forma fiable de quedarnos solo con la del
    producto es **filtrar por el EAN buscado**. Si ninguna imagen contiene el EAN,
    devolvemos [] (mejor sin imagen que poner una de un relacionado)."""
    ordered: list = []
    seen: set = set()

    def _add(raw_url: str):
        if not raw_url or raw_url.startswith("data:"):
            return
        full = urljoin(page_url, raw_url)
        if not full.startswith("http"):
            return
        if not _should_keep_url(full):
            return
        clean = _upgrade_url(full)
        if clean in seen:
            return
        seen.add(clean)
        ordered.append(clean)

    # Subir arriba y esperar para forzar la carga lazy de la imagen principal
    try:
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(800)
    except Exception:
        pass

    # 1. og:image
    try:
        for sel in ("meta[property='og:image']",
                    "meta[property='og:image:secure_url']",
                    "meta[name='twitter:image']"):
            el = page.query_selector(sel)
            if el:
                val = el.get_attribute("content") or ""
                if val:
                    log.info(f"    [distrivet] og:image: {val}")
                _add(val)
    except Exception:
        pass

    # 2. <img> del DOM (src + atributos lazy + data-zoom del visor con zoom)
    try:
        img_data = page.evaluate(r"""() => {
            return Array.from(document.querySelectorAll('img')).map(img => ({
                src:        img.getAttribute('src')            || '',
                dataSrc:    img.getAttribute('data-src')       || '',
                dataOrig:   img.getAttribute('data-original')  || '',
                dataLazy:   img.getAttribute('data-lazy-src')  || img.getAttribute('data-lazy') || '',
                srcset:     img.getAttribute('srcset')         || img.getAttribute('data-srcset') || '',
                dataZoom:   img.getAttribute('data-zoom-image')|| img.getAttribute('data-zoom') || '',
                dataLarge:  img.getAttribute('data-large_image')|| img.getAttribute('data-large') || '',
            }));
        }""")
        for item in (img_data or []):
            srcset = item.get("srcset", "")
            parts = [p.strip() for p in srcset.split(",") if p.strip()]
            if parts:
                _add(parts[-1].split()[0])
            for key in ("dataZoom", "dataLarge", "dataOrig", "dataLazy",
                        "dataSrc", "src"):
                _add(item.get(key, ""))
    except Exception as e:
        log.info(f"    [distrivet] DOM imgs error: {e}")

    # 3. background-image y data-zoom de cualquier elemento (la foto del producto
    #    de Distrivet va en un visor con zoom y NO es un <img> normal).
    try:
        bg_urls = page.evaluate(r"""() => {
            const out = [];
            for (const el of document.querySelectorAll('*')) {
                const bg = (el.style && el.style.backgroundImage) ||
                           getComputedStyle(el).backgroundImage || '';
                const m = bg && bg.match(/url\(["']?([^"')]+)["']?\)/);
                if (m) out.push(m[1]);
                for (const at of ['data-zoom-image','data-zoom','data-large','data-image','data-big','href']) {
                    const v = el.getAttribute && el.getAttribute(at);
                    if (v && /\.(jpe?g|png|webp)/i.test(v)) out.push(v);
                }
            }
            return out;
        }""")
        for u in (bg_urls or []):
            _add(u)
    except Exception as e:
        log.info(f"    [distrivet] bg-image scan error: {e}")

    # 4. HTML crudo: cualquier URL de imagen embebida en atributos/JS/estilos.
    try:
        html = page.content()
        for u in re.findall(r'[^\s"\'()<>]+\.(?:jpe?g|png|webp)', html, re.I):
            _add(u)
    except Exception:
        pass

    # 5. Filtro por EAN (o REF de la URL): las imágenes del producto llevan el EAN
    #    en el nombre; algunas fichas usan la REF (p. ej. ORD201). Descarta relacionados.
    m = re.search(r'noproducto=([^&\s]+)', (page_url or ""), re.I)
    ref = (m.group(1) if m else "").strip()
    keys = [k for k in (ean, ref) if k]
    if keys:
        sel = [u for u in ordered
               if any(k.lower() in u.lower() for k in keys)]
        # Volcar candidatas con secuencias largas de dígitos (EAN-like) para diagnóstico
        eanlike = [u for u in ordered if re.search(r'\d{8,}', u)]
        log.info(f"    [distrivet] candidatas={len(ordered)} (EAN-like={len(eanlike)}); "
                 f"claves={keys}")
        for u in eanlike[:25]:
            log.info(f"    [distrivet][diag][img] {u}")
        if sel:
            log.info(f"    [distrivet] filtro {keys}: {len(ordered)} → {len(sel)}")
            return sel
        log.warning(f"    [distrivet] NINGUNA imagen contiene EAN/REF {keys} entre "
                    f"{len(ordered)} candidatas → no pongo relacionados.")
        return []

    log.info(f"    [distrivet] URLs candidatas: {len(ordered)}")
    return ordered


def _page_contains_ean(page, ean: str) -> bool:
    """Sanity check: la ficha abierta debe contener el EAN buscado (evita
    adjuntar la imagen de un producto equivocado)."""
    if not ean:
        return True
    try:
        html = page.content()
        return ean in html
    except Exception:
        return True


# ─── Búsqueda por EAN ────────────────────────────────────────────────────────

def _find_search_input(page):
    def _qs(s):
        try:
            return page.query_selector(s)
        except Exception:
            return None
    # Evitar el filtro de direcciones de bienvenido.asp (no es el buscador)
    if _ENV_SEARCH_SEL:
        return _qs(_ENV_SEARCH_SEL)
    for sel in ("input[type='search']",
                "input[name*='busc' i]", "input[id*='busc' i]",
                "input[placeholder*='busc' i]",
                "input[name*='search' i]", "input[id*='search' i]",
                "input[placeholder*='search' i]",
                "input[name*='ean' i]", "input[placeholder*='ean' i]",
                "input[name*='referencia' i]", "input[name*='articulo' i]",
                "input[name*='producto' i]", "input[id*='producto' i]",
                "input[name*='palabra' i]", "input[name*='texto' i]",
                "input[name='q']"):
        # 'filtroDirecciones' de bienvenido.asp matchea 'busc'? no, pero por si
        # acaso lo excluimos explícitamente.
        el = _qs(sel)
        if el:
            try:
                if (el.get_attribute("id") or "") == "filtroDirecciones":
                    continue
            except Exception:
                pass
            return el
    return None


def _open_first_result(page, ean: str) -> bool:
    """En la página de resultados, abre la ficha del producto que coincide con
    el EAN. Devuelve True si navegó a una ficha (o si ya estábamos en una).

    La ficha de Distrivet es webshop-producto.asp?NoProducto={ref}, así que se
    priorizan los enlaces a ese patrón; entre varios, el de la fila que contiene
    el EAN buscado."""
    cur = page.url
    # 0. Si la búsqueda ya nos dejó en una ficha (resultado único / redirección)
    if PRODUCT_URL_PART in (cur or "").lower():
        log.info(f"    [distrivet] ya en ficha: {cur}")
        return True

    # 1. Override por env
    href = ""
    if _ENV_PRODUCT_LINK_SEL:
        el = page.query_selector(_ENV_PRODUCT_LINK_SEL)
        if el:
            href = el.get_attribute("href") or ""

    # 2. Enlace a la ficha (webshop-producto.asp), priorizando la fila del EAN
    if not href:
        try:
            href = page.evaluate("""([ean, part]) => {
                const prod = Array.from(document.querySelectorAll('a[href]'))
                    .filter(a => (a.getAttribute('href')||'').toLowerCase().includes(part));
                // a) enlace de ficha cuya fila/tarjeta contiene el EAN
                for (const a of prod) {
                    let n = a;
                    for (let up = 0; up < 7 && n; up++) {
                        if (n.innerText && n.innerText.includes(ean)) return a.getAttribute('href');
                        n = n.parentElement;
                    }
                }
                // b) primer enlace de ficha de la página de resultados
                if (prod.length) return prod[0].getAttribute('href');
                // c) cualquier enlace cuyo href contenga el EAN
                for (const a of Array.from(document.querySelectorAll('a[href]'))) {
                    const h = a.getAttribute('href') || '';
                    if (h.includes(ean)) return h;
                }
                return '';
            }""", [ean, PRODUCT_URL_PART])
        except Exception:
            href = ""

    if href and not href.lower().startswith("javascript:") and href != "#":
        target = urljoin(cur, href)
        try:
            resp = page.goto(target, timeout=30000, wait_until="domcontentloaded")
            log.info(f"    [distrivet] ficha: {target} → HTTP "
                     f"{resp.status if resp else '?'}")
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(1000)
            return True
        except Exception as e:
            log.info(f"    [distrivet] no pude abrir la ficha {target}: {e}")
            return False

    # Sin enlace claro: puede que ya estemos en la ficha (resultado único)
    log.info("    [distrivet] sin enlace de ficha claro — uso la página actual")
    return True


def _product_hrefs(page) -> list:
    """Hrefs absolutos a fichas (webshop-producto.asp) presentes ahora, orden DOM."""
    try:
        base = page.url
        hrefs = page.evaluate("""(part) =>
            Array.from(document.querySelectorAll('a[href]'))
                 .map(a => a.getAttribute('href') || '')
                 .filter(h => h.toLowerCase().includes(part))
        """, PRODUCT_URL_PART)
    except Exception:
        return []
    out, seen = [], set()
    for h in (hrefs or []):
        try:
            full = urljoin(base, h)
        except Exception:
            full = h
        if full and full not in seen:
            seen.add(full)
            out.append(full)
    return out


def _pick_product_href(page, hrefs: list, ean: str) -> str:
    """De una lista de hrefs de ficha, elige el del EAN: primero el href que
    contenga el EAN; si no, el primero (el sanity check del EAN en la ficha valida)."""
    if not hrefs:
        return ""
    for h in hrefs:
        if ean and ean in h:
            return h
    return hrefs[0]


def _autocomplete_items(page, before_set) -> list:
    """Devuelve [{href, text}] de enlaces de ficha NUEVOS (no en before_set), con
    el texto de su fila — para diagnóstico y para elegir la ficha del EAN."""
    try:
        base = page.url
        raw = page.evaluate("""(part) =>
            Array.from(document.querySelectorAll('a[href]'))
                 .filter(a => (a.getAttribute('href')||'').toLowerCase().includes(part))
                 .map(a => ({
                     href: a.getAttribute('href') || '',
                     text: ((a.closest('li,tr,div') || a).innerText || a.innerText || '').trim().slice(0,90)
                 }))
        """, PRODUCT_URL_PART)
    except Exception:
        return []
    out, seen = [], set()
    for it in (raw or []):
        try:
            full = urljoin(base, it.get("href", ""))
        except Exception:
            full = it.get("href", "")
        if not full or full in seen or full in before_set:
            continue
        seen.add(full)
        out.append({"href": full, "text": it.get("text", "")})
    return out


def _dump_autocomplete(page, ean: str, items: list):
    """Diagnóstico del autocomplete: fichas nuevas (href + texto); si no hay,
    vuelca contenedores candidatos a dropdown para fijar el selector."""
    pre = "[distrivet][diag][autocomplete]"
    log.info(f"{pre} EAN={ean} fichas nuevas: {len(items)}")
    for it in items[:12]:
        log.info(f"{pre} nuevo: {it.get('href')}  txt={it.get('text')!r}")
    if items:
        return
    try:
        info = page.evaluate(r"""() => {
            const sel = '[class*="result"],[class*="autocomplet"],[class*="suggest"],'
                      + '[class*="dropdown"],[id*="result"],[id*="autocomplet"],[id*="suggest"]';
            return Array.from(document.querySelectorAll(sel)).slice(0,6).map(c => ({
                tag: c.tagName.toLowerCase(), cls: c.className || '', id: c.id || '',
                a: Array.from(c.querySelectorAll('a[href]')).slice(0,5).map(a=>a.getAttribute('href')||''),
                txt: (c.innerText||'').trim().slice(0,120)
            }));
        }""")
        for c in (info or []):
            log.info(f"{pre} cont <{c.get('tag')}> cls={c.get('cls')!r} id={c.get('id')!r} "
                     f"a={c.get('a')} txt={c.get('txt')!r}")
    except Exception as e:
        log.info(f"{pre} no pude volcar dropdown: {e}")


def images_for_ean(ean: str) -> list:
    """Busca el EAN en Distrivet y devuelve la lista de URLs de imagen de la
    ficha (a máxima resolución posible). Cachea el resultado por EAN."""
    ean = (ean or "").strip()
    if not ean:
        return []

    cache = _load_cache()
    if ean in cache:
        hit = cache[ean]
        imgs = hit.get("images", [])
        log.info(f"    [distrivet] caché EAN {ean}: "
                 f"{'sin resultado' if not hit.get('found') else f'{len(imgs)} URLs'}")
        return imgs

    page = _get_page()
    if page is None:
        return []

    images: list = []
    product_url = ""
    try:
        # Asegurar que estamos en una página con buscador; si no, ir a la home
        # del webshop (tras pasar el login + selección de dirección).
        search = _find_search_input(page)
        if search is None:
            page.goto(SHOP_URL, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            search = _find_search_input(page)

        _dump_structure(page, "search")
        if not search:
            log.warning(f"    [distrivet] no encontré el buscador. Revisa "
                        f"[distrivet][diag][search] y fija DISTRIVET_SEARCH_SEL.")
        else:
            # Buscador autocomplete JS (#main-search, input sin name, form action='').
            # Escribimos el EAN, esperamos a que el dropdown se ESTABILICE (debounce;
            # no leer estados intermedios del tecleo) y abrimos cada ficha candidata
            # NUEVA verificando que contiene el EAN — la URL usa la REF (p.ej. ORD201),
            # no el EAN, así que no se puede elegir por href.
            before = set(_product_hrefs(page))
            try:
                search.click()
            except Exception:
                pass
            try:
                search.fill("")
            except Exception:
                pass
            try:
                search.type(ean, delay=50)
            except Exception:
                try:
                    search.fill(ean)
                except Exception:
                    pass
            page.wait_for_timeout(3500)           # debounce + AJAX del autocomplete
            try:
                page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass

            items = _autocomplete_items(page, before)
            _dump_autocomplete(page, ean, items)
            cands = [it["href"] for it in items]

            # Fallback: sin candidatas nuevas → Enter por si va a una página de resultados.
            if not cands:
                try:
                    search.press("Enter")
                    page.wait_for_load_state("networkidle", timeout=8000)
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
                if PRODUCT_URL_PART in (page.url or "").lower():
                    cands = [page.url]
                else:
                    cands = [h for h in _product_hrefs(page) if h not in before]

            # Abrir cada candidata y quedarnos con la PRIMERA que contiene el EAN.
            chosen = ""
            for cand in cands[:8]:
                if PRODUCT_URL_PART in (page.url or "").lower() and cand == page.url:
                    pass
                else:
                    try:
                        page.goto(cand, timeout=30000, wait_until="domcontentloaded")
                    except Exception:
                        continue
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(800)
                if _page_contains_ean(page, ean):
                    chosen = page.url
                    log.info(f"    [distrivet] ficha con EAN {ean}: {chosen}")
                    break
                log.info(f"    [distrivet] candidata sin EAN, descarto: {cand}")

            _dump_structure(page, "product")
            if chosen:
                product_url = chosen
                images = _extract_images(page, page.url, ean)
                if not images:
                    log.warning(f"    [distrivet] ficha correcta pero sin imagen con "
                                f"EAN {ean} (revisa [distrivet][diag][img])")
            else:
                log.warning(f"    [distrivet] ninguna candidata ({len(cands)}) contiene "
                            f"el EAN {ean}. Revisa [distrivet][diag][autocomplete].")
    except Exception as e:
        log.warning(f"    [distrivet] error buscando EAN {ean}: {e}")

    cache[ean] = {
        "ean": ean,
        "url": product_url,
        "images": images,
        "found": bool(images),
        "fecha": time.strftime("%Y-%m-%d"),
    }
    save_cache()
    return images
