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

### Caso C — Web oficial + Amazon (mejor resolución)

Con fuente `web_y_amazon` el script recolecta imágenes de **ambas** fuentes
para cada producto y combina los resultados:

1. **Web oficial** — vía el scraper `marcas/{slug}.py` (igual que Caso B).
2. **Amazon.es** — `core/amazon.py` busca la ficha del producto vía DDG
   (`site:amazon.es {título}`, fallback por EAN), navega con Playwright y
   extrae la galería principal. Sube cada URL a su **resolución original**
   quitando el token de tamaño de Amazon (`._AC_SX679_` → original ~1500px+).
3. **Dedup perceptual (pHash)** — `dedupe_images()` agrupa la misma imagen
   entre fuentes y **conserva la de mayor resolución**. Así, si Amazon tiene
   una versión mejor que la web oficial (o viceversa), se queda la mejor.
4. **DDG genérico** — último recurso solo si web + Amazon no dan nada.

Todas las imágenes pasan el filtro de resolución mínima (≥800px) antes del
dedup. **No** se baja el mínimo: preferimos menos imágenes pero de calidad.

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

## Metacampos de fuente (URL del fabricante)

Para que los workflows no busquen la fuente por DDG en cada ejecución (lento y
propenso a falsos positivos), cada producto guarda su URL oficial en metacampos
de Shopify. Sirve tanto al workflow de imágenes como al futuro de descripciones.

| Metacampo | Tipo | Para qué |
|---|---|---|
| `fuentes.url_fabricante` | `url` | URL **activa** que leen los workflows |
| `fuentes.url_fabricante_2` | `url` | URL **alternativa** opcional (otra versión, idioma o web extendida del mismo producto) |
| `fuentes.historico` | `json` | Registro append-only `[{url, fecha, workflow, resultado}]` |

**Por qué metacampo y no la descripción (`body_html`):** la descripción es
visible al cliente, es HTML libre (parseo frágil) y la sobrescribiría el futuro
workflow de descripciones. El metacampo es estructurado, tipado, invisible al
storefront y accesible por API (`/products/{id}/metafields.json`).

**Crear las definiciones** (workflow `Crear metacampos fuente` / `--crear-metacampos`):
crea vía GraphQL las definiciones de los tres metacampos para que aparezcan en el
admin del producto y se puedan pegar las URLs a mano. Ejecutar una sola vez.

**Cómo funciona** (en `core/process_brand.py` → `run_web`):
1. Antes del matching se leen `fuentes.url_fabricante` y `url_fabricante_2`. Si
   hay alguna **y** el scraper expone `scrape_product_url(url, barcode)` → se
   scrapean esas URLs directamente, **saltando DDG/matching** (override sobre
   cualquier fuente web). Con dos URLs se combinan las imágenes y el dedup
   perceptual conserva la de mayor resolución.
2. Si no hay URL → flujo normal (DDG/`find_best_match`), y al resolverla se
   **escribe** `url_fabricante` (auto-aprendizaje) + entrada en el histórico.

**Backfill inicial** (workflow `Backfill URLs fabricante` / `--backfill-urls`):
importa las URLs ya cacheadas en `resultados/{slug}_catalog.json` a los
metacampos sin lanzar DDG. Requiere que el scraper exponga `title_cache_key(title)`.

**Resolver URLs desde cero** (workflow `Resolver URLs fabricante` / `--resolver-urls`):
cuando todavía no hay URLs cacheadas, busca la URL oficial de **cada** producto
con el scraper de la marca (`find_best_match`: slug directo + DDG) y la escribe en
`fuentes.url_fabricante` + histórico. **No procesa imágenes** — solo resuelve y
guarda la URL. Requiere `marcas/{slug}.py` (Playwright). Inputs: `vendor`,
`web_url`, `product_ids` (opcional), `rebuild_catalog`. Usado para poblar las URLs
de Applaws (`applaws.pet/producto/{slug}/`).

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

Opcionales para aprovechar los metacampos de fuente:

```python
def scrape_product_url(url: str, barcode: str = "") -> dict | None:
    """Extrae imágenes de una URL exacta (sin DDG). {name, url, images} o None.
    Habilita el override por metacampo fuentes.url_fabricante."""
    ...

title_cache_key = _title_key   # alias público: título Shopify → clave de caché
                               # (lo usa --backfill-urls para importar URLs)
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
- **PrestaShop — thumbnails del carrusel:** el DOM scan captura thumbnails 322×383 de productos del carrusel lateral. Los filtros WooCommerce (`.related`, `.upsells`) no aplican. Añadir en el JS: `.product-miniature`, `.js-product-miniature`, `[class*="miniature"]`, `[class*="product-list"]` + filtro `naturalWidth < 200`.
- **PrestaShop — upgrade de tamaño antes de descargar:** las imágenes de galería se sirven como `medium_default` (~452px) en el DOM → fallan el mínimo de 800px. Solución: `_upgrade_prestashop_url()` convierte `medium/small/home/category_default` → `large_default` (~800-1000px) antes de añadir a la lista de descarga. Implementado en `_add()` antes del dedup.
- **TRAMPA**: no normalizar la URL PrestaShop quitando el tamaño (`/924/img.jpg`) en la lista de descarga — esa URL no existe en el servidor. Solo subir el tamaño (medium→large), nunca quitar el sufijo completamente. El `_strip_size_suffix` debe tocar solo el patrón WordPress `-WxH`.

### Pipeline de imagen — lecciones

- **fills-width (padding solo vertical):** imágenes PrestaShop son 1000×1188 donde el producto toca los bordes laterales pero tiene espacio blanco arriba/abajo. `autocrop_white` quita el blanco vertical (`fill_h ≈ 0.84 < 0.85`) pero `fill_w = 1.0`. La condición `fills-frame` (`fill_w > 0.85 AND fill_h > 0.85`) no se cumple → antes se añadía 5% de padding en TODOS los lados. Fix en `image_utils.py`: nuevo caso `elif fill_w >= 0.98` → `use_padding = False` + usa imagen croppeada. El centrado en canvas 2000×2000 ya proporciona el margen vertical natural.
- **Umbral fills-width:** `0.98` (solo 2% de margen de tolerancia). Si el ancho cambia más del 2% tras autocrop, el producto no toca los laterales y el padding uniforme es correcto.

### Amazon como fuente (web_y_amazon) — `core/amazon.py`

**Orden de métodos de búsqueda de imagen:**

1. **Google Custom Search API** (primario, cuando `GOOGLE_API_KEY` + `GOOGLE_CSE_ID` definidos):
   - 100 queries/día gratis. Resultados de Google, más precisos que Bing/DDG.
   - Setup: https://programmablesearchengine.google.com/ → crear CSE (cx) +
     Google Cloud Console → habilitar "Custom Search API" → crear API key.
   - Añadir `GOOGLE_API_KEY` y `GOOGLE_CSE_ID` a los secrets de GitHub Actions.
   - Query: `site:amazon.es {título}` con `searchType=image&imgSize=large`.
   - Las URLs devueltas son del CDN de Amazon → `strip_size_token()` → original.
2. **DDG image search** (secundario, sin API key, cualquier IP):
   - Usa Bing como backend. Devuelve URLs del CDN de Amazon directamente.
   - `AMAZON_MATCH_THRESHOLD = 0.35` filtra imágenes de productos distintos.
3. **Playwright** (opt-in `AMAZON_USE_PLAYWRIGHT=1`, solo IP residencial).

**Lecciones de matching de imagen Amazon:**
- **Tokenización número+unidad:** "250ml" en Shopify vs "250 ml" en DDG title → `ml`
  desaparece como stopword y los tokens son "250ml" ≠ "250" → similitud reducida.
  Fix en `_norm()`: `re.sub(r'(\d+)(ml|gr|kg|mg|cl|l|cm|mm|g)\b', r'\1 \2', text)`.
  Convierte "250ml" → "250 ml" antes del split → ambos normalizan a "250" → coinciden.
- **Resolución original:** token de tamaño `._AC_SX679_`, `._SL1500_`, `._AC_UL320_`...
  Quitar con `re.sub(r'\._[A-Z0-9][A-Z0-9_,]*_\.', '.', url)` → imagen original (≥1500px).
- **Extracción DOM (Playwright):** `data-a-dynamic-image` (JSON url→[w,h]) + `data-old-hires`
  + `src` de `#imgTagWrapperId`, `#landingImage`, `#altImages`. Dedup por ID de imagen.
- **Anti-bot Playwright:** mismo patrón (Chrome UA, Sec-Fetch-*, bypass webdriver, warm-up).
  Amazon puede mostrar CAPTCHA desde IPs de datacenter — por eso DDG/Google CSE son preferibles.
- **Comparación entre fuentes = pHash.** `dedupe_images` identifica la misma imagen entre
  web y Amazon y conserva la de mayor resolución, sin coste de API.

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
| Applaws | Applaws | Completado (imágenes) — **pendiente** poblar `fuentes.url_fabricante` (web oficial) | web_oficial → marcas/applaws.py (applaws.pet/producto/) |
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
| Menforsan | MENFORSAN | **En proceso** — fuente `web_y_amazon` probada, fixes aplicados, pendiente proceso masivo | web_y_amazon → marcas/menforsan.py (menforsan.com) |

### Applaws — notas de estrategia (URLs fabricante)

**Objetivo:** poblar `fuentes.url_fabricante` de cada producto Applaws con su URL
oficial en `applaws.pet` (las imágenes ya están hechas). Útil para el futuro
workflow de descripciones y para reprocesar imágenes desde la web si hace falta.

**Scraper `marcas/applaws.py`:**
- CMS: **WooCommerce** en español — URLs `/producto/{slug}/`. El slug incluye el
  peso (`applaws-cat-dry-kitten-pollo-2kg`), que no siempre está en el título Shopify.
- Anti-bot: `applaws.pet` devuelve **HTTP 403** sin navegador → Playwright + Chrome
  UA + headers `Sec-Fetch-*` + bypass `navigator.webdriver` + warm-up homepage.
- Resolución por producto: (1) slug directo desde el título; (2) DDG
  `site:applaws.pet/producto/` con cascada (sin marca / por EAN) y ranking del h1.
- `_normalize` divide número+unidad (`2kg`→`2 kg`) y descarta la unidad como
  stopword → el peso no rompe el matching. `MATCH_THRESHOLD = 0.30`.

**Cómo ejecutar:** workflow `Resolver URLs fabricante` (`--resolver-urls`) con
`vendor=Applaws`, `web_url=https://applaws.pet/`. Test con un `product_ids` antes
del lote completo. Escribe solo el metacampo, no toca imágenes.

**Infra:** el workflow hace checkout de `main`; el scraper + el modo `--resolver-urls`
deben estar en `main` antes de lanzarlo.

### Menforsan — notas de estrategia (EN PROCESO)

**Situación:** los productos MENFORSAN no tienen imágenes en Shopify → Caso C
(web oficial + Amazon). La web `menforsan.com` devuelve **HTTP 403** a peticiones sin
navegador (igual que beaphar.es).

**Estado del scraper:**
- `marcas/menforsan.py` en `main` con anti-bot completo.
- CMS: **PrestaShop** — URLs `/es/{categoria}/{id}-{slug}.html`.
- `_is_product_url()` requiere `/es/` en el path (evita URLs en inglés) + patrón `\d+-.+\.html`.
- Playwright con headers Sec-Fetch-*, bypass `navigator.webdriver`, warm-up homepage.
- DDG text search con `site:menforsan.com/es/` (no `site:menforsan.com` para evitar `/en/`).
- `MATCH_THRESHOLD = 0.30`. `CATALOG_PATH = resultados/menforsan_catalog.json`.

**Fuente activa: `web_y_amazon`**

**Historial de fixes (todos en `main`):**

| Fix | Descripción |
|---|---|
| DDG query `/es/` | Evita que DDG devuelva páginas en inglés (score 0.12 → descartadas) |
| Trailing slash `.html` | PrestaShop da 404 si URL termina en `.html/` |
| DOM filter PrestaShop | `.product-miniature` + `naturalWidth < 200` elimina thumbnails 322×383 |
| Amazon DDG title filter | `AMAZON_MATCH_THRESHOLD = 0.35`; skip si sim < umbral |
| Tokenización número+unidad | `"250ml"` → `"250 ml"` en `_norm()` para que coincida con `"250 ml"` del DDG title |
| Google CSE primario | Cuando `GOOGLE_API_KEY` + `GOOGLE_CSE_ID` definidos, usa Google en vez de Bing |
| fills-width sin padding | Si autocrop no elimina ancho (≥98%) pero sí altura → sin padding lateral |

**Tests exitosos:**
- `15509651259779` CHAMPU BIOTINA PARA CABALLO 1L: score=1.00, 6 imgs web
- `15509653815683` INSECTICIDA AVES SPRAY 250ML: score=0.43, 6 imgs web, Amazon filter OK
- `15509653848451` (pendiente verificar resultado tras fix padding lateral)

**Próximos pasos:**
1. **Verificar test** con producto `15509653848451` para confirmar fix padding
2. **Activar Google CSE** (opcional): añadir `GOOGLE_API_KEY` + `GOOGLE_CSE_ID` a secrets de GitHub para mejores resultados Amazon
3. **Proceso masivo** → `Procesar imágenes de marca`

**Parámetros workflow `Test marca`:**

| Campo | Valor |
|---|---|
| `vendor` | `MENFORSAN` |
| `fuente` | `web_y_amazon` |
| `web_url` | `https://www.menforsan.com/` |
| `product_id` | *(cualquier ID de producto MENFORSAN)* |
| `rebuild_catalog` | `false` |
| `pipeline` | `standard` |
| `force_padding` | `auto` |

**Parámetros workflow `Procesar imágenes de marca`:**
vendor=MENFORSAN, fuente=web_y_amazon, web_url=https://www.menforsan.com/,
rebuild_catalog=false, pipeline=standard, force_padding=auto.
Reintentar fallidos por lote con `product_ids` si quedan sin match.

**Recordatorio de infra:** los workflows hacen checkout de `main`. Todos los fixes
ya están en `main` — no hace falta cherry-pick.

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

## Trabajo pendiente / ideas descartadas temporalmente

### Restaurar calidad de imágenes degradadas (PENDIENTE)

**Objetivo:** workflow para recuperar nitidez en imágenes que perdieron calidad
por compresión, reescalado o ediciones repetidas.

**Pipeline diseñado (implementado y revertido — demasiado lento en Actions):**
1. Denoise — `cv2.fastNlMeansDenoisingColored` elimina ruido/artefactos JPG *antes* de ampliar
2. Super-resolución EDSR x2/x4 — elimina pixelado, recupera detalle (`core/upscale.py` ya lo tiene)
3. Unsharp mask — `PIL.ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3)` sube nitidez
4. `process_image` → estándar 2000×2000 WebP

**Motivo del revert:** ejecución muy lenta en GitHub Actions + resultado no satisfactorio.

**Próximo paso cuando se retome:**
- Probar **Real-ESRGAN** en lugar de EDSR: está entrenado en degradación real
  (compresión, ruido), recupera más detalle en imágenes realmente degradadas.
  Paquete: `basicsr` + `realesrgan` (PyTorch CPU) o binario `realesrgan-ncnn-vulkan`.
- Origen: descarga imagen viva de Shopify por vendor + product_ids.
- Destino: reemplaza en la misma posición conservando alt.
- Módulo de referencia a recrear: `core/restore_image.py` (eliminado del repo).

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
