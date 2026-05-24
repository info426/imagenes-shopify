# Shopify Product Images — Guía de trabajo

Repositorio para actualizar las imágenes de productos Shopify con las imágenes
oficiales de cada marca, procesadas al estándar de la tienda.

Tienda: `7ev1zx-eg.myshopify.com`

---

## Estructura del repositorio

```
imagenes-shopify/
├── .github/workflows/
│   ├── backup_marca.yml      ← paso 1: descarga originales de Shopify → Git LFS
│   ├── test_marca.yml        ← paso 2: prueba con un producto concreto
│   └── procesar_marca.yml    ← paso 3: proceso masivo de todos los productos
│
├── core/
│   ├── image_utils.py        ← lógica de imagen compartida
│   ├── shopify_api.py        ← cliente Shopify REST API
│   └── process_brand.py      ← orquestador principal (punto de entrada)
│
├── marcas/
│   └── acana.py              ← scraper Playwright para emea.acana.com
│   (añadir un .py por cada marca que necesite scraping web)
│
├── backups/                  ← imágenes originales de Shopify (Git LFS)
│   └── {vendor_slug}/{product_id}/img_01.jpg ...
│
├── resultados/               ← catálogos web en caché + logs de proceso
│   └── acana_catalog.json
│
└── requirements.txt
```

---

## Flujo de trabajo estándar por marca

### Caso A — La marca ya tiene imágenes en Shopify

1. **Backup** → ejecutar `Backup imágenes de marca` con el vendor
   - Descarga todas las imágenes a `backups/{slug}/`
   - Las guarda en Git con LFS (backup permanente antes de sobrescribir)

2. **Test** → ejecutar `Test marca` con un `product_id` concreto
   - Fuente: `shopify_backup`
   - Verificar resultado en Shopify

3. **Proceso masivo** → ejecutar `Procesar imágenes de marca`
   - Fuente: `shopify_backup`
   - Lee de `backups/{slug}/`, procesa, reemplaza en Shopify

### Caso B — La marca NO tiene imágenes en Shopify (web oficial)

1. Crear `marcas/{slug}.py` con `scrape_catalog()` y `find_best_match()`
2. **Test** → ejecutar `Test marca` con fuente `web_oficial` + `web_url`
3. **Proceso masivo** → ejecutar `Procesar imágenes de marca`

### Caso C — Web oficial + otras fuentes (Amazon, etc.)

Igual que B pero con fuente `web_y_amazon`. Si la web oficial no tiene
imágenes de alta resolución para un producto, el script busca en DuckDuckGo
imágenes externas de alta resolución (≥800px).

---

## Los 3 workflows

| Workflow | Descripción | Inputs clave |
|---|---|---|
| `Backup imágenes de marca` | Descarga originales Shopify → Git LFS | `vendor` |
| `Test marca` | Prueba con un producto | `vendor`, `fuente`, `product_id`, `web_url`* |
| `Procesar imágenes de marca` | Proceso masivo | `vendor`, `fuente`, `web_url`*, `product_ids`* |

`*` opcional según fuente

**Fuentes disponibles:**
- `shopify_backup` — lee de `backups/{slug}/` (marcas con imágenes en Shopify)
- `web_oficial` — scrapea web del fabricante (requiere `marcas/{slug}.py`)
- `web_y_amazon` — web oficial + búsqueda DDG adicional

---

## Estándar de imagen

Todas las imágenes se procesan con:
- **Formato**: WebP, calidad 90, method 6 (óptimo para Core Web Vitals)
- **Dimensiones**: 2000×2000 px, imagen centrada sobre canvas blanco
- **Fondo transparente**: se convierte a fondo blanco
  - >15% píxeles transparentes → composite_on_white (producto sobre fondo transp.)
  - ≤15% transparentes → fill_transparent_with_blur (esquinas decorativas)
- **Padding**: 5% si las esquinas son blancas (≥60% píxeles blancos); sin padding si es ilustración
- **Resolución mínima para imágenes web**: 800×800 px (las menores se descartan)

---

## Añadir scraper para una nueva marca

1. Crear `marcas/{slug}.py` con estas dos funciones:

```python
def scrape_catalog(web_url: str, rebuild: bool = False) -> dict:
    """
    Devuelve { handle: { name, url, images: [url, ...] } }
    Cachea en resultados/{slug}_catalog.json
    """
    ...

def find_best_match(shopify_title: str, catalog: dict) -> tuple:
    """Devuelve (handle, score). score mínimo aceptable: 0.10"""
    ...
```

2. Ejecutar `Test marca` con `fuente: web_oficial` para validar

---

## Algoritmo de matching (Jaccard)

Compara tokens del título Shopify con tokens del handle+nombre del catálogo web.

1. Normalizar: minúsculas, sin acentos, sin puntuación
2. Eliminar stopwords e IGNORE_TOKENS de la marca (pesos, categorías, etc.)
3. Score = intersección / unión de tokens (Jaccard)
4. Umbral mínimo: 0.10

---

## Experiencia técnica acumulada

Esta sección se actualiza con cada marca procesada. Consultar siempre al escribir
un nuevo scraper o diagnosticar fallos.

### CMS detectados y patrones de URL de producto

| CMS | Patrón URL producto | Ejemplo | Cómo detectar |
|---|---|---|---|
| WooCommerce | `/{slug}/` o `/producto/{slug}/` | `menforsan.com/producto/champu-perros-400ml/` | URL sin extensión, path corto |
| PrestaShop | `/es/{categoria}/{id}-{slug}.html` | `menforsan.com/es/champus/248-champu-biotina.html` | Extensión `.html`, segmento `{numero}-{texto}` |
| PrestaShop (EN) | `/en/{category}/{id}-{slug}.html` | igual pero `/en/` | Mismo patrón, idioma diferente |
| WooCommerce multidioma | `/es/producto/{slug}/` | artero.com/es/petcare/{slug}/ | Prefijo de idioma + `/producto/` |

**Regla para `_is_product_url()`:** hacer el filtro lo más estricto posible.
DDG devuelve páginas de categoría, marca y blog mezcladas con productos.
Cada URL que pasa el filtro cuesta una navegación Playwright (lenta y costosa).
- PrestaShop: `re.match(r'^\d+-.+\.html$', last_segment)` — probado en menforsan.com
- WooCommerce: comprobar que el path tiene el prefijo correcto Y no es categoría/tag/blog

### Protección anti-bot y cómo superarla

| Sitio / patrón | Protección | Solución probada | Resultado |
|---|---|---|---|
| beaphar.es | HTTP 403 sin navegador | Playwright + Chrome UA + `wait_until="networkidle"` | ✅ Funciona |
| menforsan.com | HTTP 403 sin navegador (PrestaShop) | Playwright + Chrome UA + headers Sec-Fetch-* + bypass `navigator.webdriver` + warm-up homepage | ✅ Funciona (test 2: warm-up OK, sin 403) |
| artero.com | Sin protección significativa | requests / Playwright básico | ✅ Funciona |

**Técnicas anti-bot (de menor a mayor agresividad):**
1. Chrome User-Agent en headers
2. `Accept-Language`, `Accept`, `Accept-Encoding` realistas
3. Headers `Sec-Fetch-*` (`Dest: document`, `Mode: navigate`, `Site: none`, `User: ?1`)
4. `ctx.add_init_script()` para ocultar `navigator.webdriver` y simular `window.chrome`
5. Warm-up: visitar la homepage primero para establecer cookies antes de navegar a productos
6. Si todo lo anterior falla: probar `playwright-stealth` (librería externa) o rotar UA

**Plantilla `_get_page()` con anti-bot completo** (usar como base para nuevos scrapers):
```python
ctx = browser.new_context(
    user_agent=USER_AGENT,
    locale="es-ES",
    viewport={"width": 1440, "height": 900},
    extra_http_headers={
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
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
# Warm-up para establecer cookies
page.goto("https://www.{dominio}/", timeout=20000, wait_until="domcontentloaded")
page.wait_for_timeout(2000)
```

### Estrategia DDG por tipo de sitio

| Situación | Estrategia | Query |
|---|---|---|
| Sitio con slug derivable del título | Slug directo → fallback DDG si 404 | `site:{dominio} {título}` |
| ID numérico no derivable (PrestaShop) | Solo DDG | `site:{dominio} {título}` |
| Título con prefijo de marca redundante | Cascada: título completo → sin prefijo | `site:{dominio} {título sin marca}` |
| Sin resultados (producto oscuro) | Fallback por EAN/barcode | `site:{dominio} {EAN}` |

**Lección aprendida (menforsan test 1):** DDG encontró la URL correcta en posición 2,
pero también devolvió 4 páginas de categoría que pasaron el filtro `_is_product_url()`.
Con el filtro PrestaShop corregido, solo quedan las URLs `.html` reales.

### Matching Jaccard — thresholds por marca

| Marca | MATCH_THRESHOLD | Observaciones |
|---|---|---|
| Beaphar | 0.34 | Productos similares (pipetas, sprays) requieren threshold alto para evitar falsos positivos |
| Menforsan | 0.30 | Punto de partida; ajustar si hay muchos sin-match o falsos positivos |
| Artero | N/A | Slug directo; el score solo valida el sanity-check del h1 |

**Cuándo bajar el threshold:** muchos productos sin match, scores reales entre 0.20-0.29.
**Cuándo subir el threshold:** falsos positivos (producto equivocado asignado).

### Extracción de imágenes — lecciones

- **Orden de prioridad:** `og:image` > JSON-LD > DOM `<img>` — `og:image` suele ser la imagen principal en buena resolución.
- **Excluir relacionados:** en WooCommerce filtrar `.related`, `.upsells`, `.cross-sells` del DOM scan.
- **Filtro EAN (Beaphar):** el CDN nombra los ficheros con el EAN (`8711231199877.jpg`). Aplicar `_filter_by_ean()` elimina imágenes de "productos relacionados" del CDN.
- **Sufijo WordPress `-WxH`:** quitar con `re.sub(r'-\d+x\d+(\.[a-z]{3,4})', r'\1', url)` para obtener la imagen original en máxima resolución.
- **PrestaShop:** las imágenes suelen estar en `/img/p/{carpetas}/{id}-{lang}.jpg`. La `og:image` las referencia directamente; es suficiente en la mayoría de casos.
- **PrestaShop — NO trailing slash en `.html`:** PrestaShop devuelve HTTP 404 si la URL termina en `.html/`. WooCommerce sí espera trailing slash. En `_ddg_query_urls`: `if not url.lower().endswith(".html"): url += "/"`
- **PrestaShop — thumbnails del carrusel:** el DOM scan captura ~44 thumbnails 322×383 de productos del carrusel lateral. Los filtros WooCommerce (`.related`, `.upsells`) no aplican. Añadir en el JS: `.product-miniature`, `.js-product-miniature`, `[class*="miniature"]`, `[class*="product-list"]` + filtro `naturalWidth < 200`.
- **PrestaShop — normalización de URL para deduplicar:** `/2694-large_default/img.jpg` y `/2694-thickbox_default/img.jpg` son el mismo producto en distinto tamaño. Normalizar con `re.sub(r'/(\d+)-(?:cart|small|medium|large|home|thickbox|category)_default/', r'/\1/', url)` para evitar duplicados en el set `seen`.

### Logging — qué nivel usar

- `log.info` para todo lo que ayuda a diagnosticar desde el artefacto del workflow (HTTP status, candidatos DDG, scores, imágenes extraídas)
- `log.debug` solo para errores de bajo nivel que no afectan al flujo (excepciones internas de Playwright, etc.)
- **Lección (menforsan test 1):** los HTTP 403 estaban en `log.debug` → invisibles en el artefacto. Cambiar siempre los errores de navegación a `log.info`.

---

## Estado de marcas

| Marca | Vendor Shopify | Estado | Fuente |
|---|---|---|---|
| Farmina N&D | Farmina | Completado | — |
| Farmina Vet Life | Farmina Vet Life | Completado | — |
| Alpha Spirit | Alpha Spirit | Completado | — |
| Applaws | Applaws | Completado | — |
| CALIBRA | CALIBRA | **Completado** — EANs corregidos (9 Joy Classic), imágenes optimizadas (todos los productos) | shopify_backup |
| Acana | Acana | En proceso | web_oficial → marcas/acana.py |
| ARTERO | ARTERO | **Listo para proceso masivo** — scraper testado OK | web_oficial → marcas/artero.py (artero.com/es/petcare/) |
| AFFINITY (ADVANCE, ADVANCE VET, LIBRA, BREKKIES, NATURAL TRAINER, NATURE'S VARIETY) | AFFINITY | Completado — backup OK, listo para optimizar imágenes | web_y_amazon → marcas/affinity.py |
| Virbac | Virbac | Pendiente backup | shopify_backup |
| Churu | Churu | Pendiente backup | shopify_backup |
| Farmina ND | Farmina | Pendiente backup | shopify_backup |
| Farmina Vet Life | Farmina Vet Life | Pendiente backup | shopify_backup |
| Lenda | Lenda | Pendiente (mixto) | shopify_backup + web_oficial |
| Beaphar | BEAPHAR | **Completado** — 126/126 productos con imágenes oficiales; unificaciones B, C, E, L, M aplicadas vía API | web_oficial → marcas/beaphar.py (beaphar.es) |
| Menforsan | MENFORSAN | **Listo para proceso masivo** — test OK (score=1.00, 2 imgs). Anti-bot + filtros PrestaShop confirmados. | web_oficial → marcas/menforsan.py (menforsan.com) |

### Menforsan — notas de estrategia (EN PROCESO)

**Situación:** los productos MENFORSAN no tienen imágenes en Shopify → Caso B
(scraper web). La web `menforsan.com` devuelve **HTTP 403** a peticiones sin
navegador (igual que beaphar.es).

**Estado actual (commit `f429d72`):**
- `marcas/menforsan.py` en `main` con anti-bot completo.
- CMS: **PrestaShop** — URLs `/es/{categoria}/{id}-{slug}.html`.
- `_is_product_url()` corregido: solo acepta patrón `\d+-.+\.html` (evita categorías).
- Playwright con headers Sec-Fetch-*, bypass `navigator.webdriver`, warm-up homepage.
- `MATCH_THRESHOLD = 0.30`. `CATALOG_PATH = resultados/menforsan_catalog.json`.

**Historial de tests (ID: 15509651259779 — CHAMPU BIOTINA PARA CABALLO 1L):**

| Test | Problema | Fix aplicado |
|---|---|---|
| Test 1 | HTTP 403 a Playwright (todos los candidatos) + filtro URL pasaba categorías | Anti-bot completo (headers Sec-Fetch-*, bypass webdriver, warm-up) + filtro `\d+-.+\.html` |
| Test 2 | Warm-up OK, sin 403. **HTTP 404** en todos — trailing slash tras `.html` | `_ddg_query_urls`: no añadir `/` si URL termina en `.html` |
| Test 3 | **ÉXITO** score=1.00, 2 imgs subidas. 44 thumbnails 322×383 descartados por baja res (DOM filter WooCommerce no aplica a PrestaShop) | DOM filter añade clases PrestaShop (`.product-miniature`, etc.) + filtro `naturalWidth < 200` |

**Estado: listo para proceso masivo.**

**Próximos pasos:**
1. **Proceso masivo** → `Procesar imágenes de marca` con los parámetros de abajo.

**Parámetros workflow `Test marca`:**

| Campo | Valor |
|---|---|
| `vendor` | `MENFORSAN` *(confirmar mayúsculas exactas en Shopify)* |
| `fuente` | `web_oficial` |
| `web_url` | `https://www.menforsan.com/` |
| `product_id` | *(cualquier ID de producto MENFORSAN)* |
| `rebuild_catalog` | `false` |
| `pipeline` | `standard` |
| `force_padding` | `auto` |

**Parámetros workflow `Procesar imágenes de marca` (cuando test OK):**
vendor=MENFORSAN, fuente=web_oficial, web_url=https://www.menforsan.com/,
rebuild_catalog=false, pipeline=standard, force_padding=auto.
Reintentar fallidos por lote con `product_ids` si quedan sin match.

**Recordatorio de infra:** los workflows hacen checkout de `main`. El scraper
ya está en `main` — no hace falta cherry-pick antes del primer test.

### Beaphar — notas de estrategia

beaphar.es usa URLs de producto con **ID numérico interno** no derivable del
título (`/product/{id}-{slug}/`), y bloquea peticiones sin navegador (HTTP 403).
Por eso el scraper:

1. **No** construye slug directo (a diferencia de Artero) — el ID es desconocido.
2. Resuelve cada producto **bajo demanda** vía DuckDuckGo con filtro
   `site:beaphar.es/product/`. Cascada de queries: título completo → sin prefijo
   "BEAPHAR" → por EAN/barcode (si faltan candidatos).
3. Navega con **Playwright** (UA de Chrome) para esquivar el 403, esperando
   `networkidle` para que WooCommerce cargue las imágenes lazy.
4. **Ranking de candidatos**: recolecta hasta 6 URLs, puntúa cada h1 con Jaccard
   + stemming de plurales ES (`gato`≈`gatos`), elige el de mayor score.
   Umbral `MATCH_THRESHOLD=0.34`. Evita falsos positivos entre productos similares.
5. **Imágenes**: og:image + JSON-LD + galería DOM (excluye .related/.upsells).
   beaphar.es sirve desde CloudFront (`d7rh5s3nxmpy4.cloudfront.net`), no filtra
   por host. **Filtro por EAN**: el CDN nombra los ficheros con el EAN del
   producto (`8711231199877.jpg`, `_1.jpg`...); descarta imágenes del CDN cuyo
   EAN no coincida (elimina las de "Productos relacionados").
6. Cachea cada hallazgo en `resultados/beaphar_catalog.json` (efímero en el runner).

El paso del barcode lo hace `core/process_brand.py` (extrae el primer EAN del
producto y lo pasa a `find_best_match` vía `inspect.signature`).

**Estado del proceso masivo (web_oficial): COMPLETADO — 126/126 con imágenes.**
- Run #68 (todos): 126 productos → 99 OK, 26 sin match, 1 sin imagen.
- Re-run de los 27 fallidos con `product_ids` (lote) tras añadir fallbacks
  sin-marca + EAN → **27/27 OK, 0 sin match, 0 sin imagen**. Los fallbacks
  recuperaron incluso los `*DX*` y los de EAN `8710729*`.
- IDs del lote de reintento (referencia): 15509650997635,15509650080131,
  15509649686915,15509649621379,15509649588611,15509649326467,15509649195395,
  15509649031555,15509648081283,15509645099395,15509644837251,15509644444035,
  15509644247427,15509644050819,15509643100547,15509643133315,15509643002243,
  15509642871171,15509641953667,15509641822595,15509641331075,15509641232771,
  15509641036163,15509640970627,15509640774019,15981868876163,15981869105539

Unificaciones aplicadas en Shopify (vía API, EANs y precios preservados):
Pipetas Repulsivas Perro (B), Cat Comfort Spray (C), Multifresh Neutralizador
Olores Gato (E), No Stress Gato (L), Calming No Stress Perro (M).

### ARTERO — notas de estrategia

El listado de `artero.com/es/petcare/` se carga vía JS dinámico y no es fiable de scrapear en bloque. El scraper usa **búsqueda bajo demanda** por producto:

1. **Caché** — si el producto ya fue resuelto en una ejecución anterior (`resultados/artero_catalog.json`), se usa directamente.
2. **Slug directo** — se genera la URL desde el título Shopify:
   - `ARTERO ACONDICIONADOR FLASH 300 ML (NDR)` → `artero.com/es/petcare/artero-acondicionador-flash-300ml`
3. **Fallback DDG** — si el slug directo devuelve 404, se busca con DuckDuckGo `site:artero.com/es/petcare/ <título limpio>`.
4. **Sanity check** — el h1 de la página encontrada debe compartir tokens con el título Shopify para evitar falsos positivos.
5. Cada hallazgo se guarda en `artero_catalog.json` para no repetir la búsqueda.

**Para el proceso masivo** (`Procesar imágenes de marca`):
- `vendor`: `ARTERO`
- `fuente`: `web_oficial`
- `web_url`: `https://artero.com/es/petcare/`
- `rebuild_catalog`: `false` (reutiliza los productos ya resueltos en el test)
- `pipeline`: `standard`
- `force_padding`: `auto`

### CALIBRA — notas de finalización

- **EANs corregidos** (workflow `Actualizar EAN CALIBRA`): 9 variantes Joy Classic con prefijo cambiado 8594062→8595706. Ver `resultados/CALIBRA_ean_discrepancias.md`.
- **Imágenes**: todos los productos procesados con `shopify_backup`. Log en `resultados/calibra_processed.json` (50 productos).
- **Pendiente revisar manualmente** (3 productos): Joy Classic LARGE FILLET CORDERO / AROS POLLO / PECHUGA POLLO → renombrados como BITS en Distrivet; verificar si el EAN ha cambiado también.
- **Sin equivalente en Distrivet** (13 productos): Joy CAT, Joy Mini, Verve Snack, Expert Energy — decidir si desactivar o buscar proveedor alternativo.

---

## Autenticación Shopify

```
POST https://{SHOP_DOMAIN}/admin/oauth/access_token
  grant_type=client_credentials
  client_id=CLIENT_ID
  client_secret=CLIENT_SECRET
```

Variables de entorno en los workflows (secrets de GitHub):
SHOP_DOMAIN, CLIENT_ID, CLIENT_SECRET
