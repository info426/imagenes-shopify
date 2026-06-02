# Shopify Product Images — Guía de trabajo

Repositorio para actualizar las imágenes de productos Shopify con las imágenes
oficiales de cada marca, procesadas al estándar de la tienda.

Tienda: `7ev1zx-eg.myshopify.com`

---

## Regla de desarrollo — SIEMPRE mergear a main

**Todos los cambios deben mergearse a `main` antes de cualquier otra cosa.**
Los workflows de GitHub Actions hacen `checkout ref: main`, por lo que cualquier
código en una rama de desarrollo no llega a los workflows hasta que esté en main.

**Flujo obligatorio al final de cada tarea:**
1. Commitear los cambios en la rama de desarrollo
2. `git checkout main && git pull origin main`
3. `git merge <rama-desarrollo> --no-edit`
4. `git push origin main`
5. Borrar la rama temporal si ya no hace falta

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

Opción `clear_on_no_match` (input `true`/`false`): si un producto queda `sin_match`,
**elimina** su metacampo `url_key` (limpia URLs erróneas de un run anterior en vez
de dejarlas). Usa `ShopifyAPI.delete_metafield`.

**Snapshot (Shopify → repo)** (workflow `Snapshot URLs fabricante` / `--snapshot-urls`):
operación **inversa** al resolver — **lee** los metacampos `url_fabricante` y
`url_fabricante_2` actuales de Shopify (la fuente de verdad tras correcciones
manuales) y los **guarda en el repo** como memoria duradera:
1. `resultados/{slug}_urls_snapshot.json` — registro auditable `[{id, title,
   url_fabricante, url_fabricante_2}]` de **todos** los productos.
2. Siembra la caché del scraper con las `url_fabricante_2` verificadas (si el
   scraper expone `seed_uk_cache(title→url)`, p. ej. Applaws): las entradas se
   indexan por clave de título y **sin `handle`** → `find_best_match` hace
   **cache-hit exacto** y devuelve la URL verificada sin volver a resolver ni
   pasar por los guards (`source: shopify_manual`).
**No escribe nada en Shopify** (solo lee) y **no usa navegador** (sin scraping).
Inputs: `vendor`, `product_ids` (opcional). Sirve para fijar el trabajo manual
como autoridad y evitar que un re-run accidental vuelva a romper las URLs.

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
| **Shopify** | `/products/{handle}/` (plural) | `applaws.com/uk/products/tuna-fillet-...` | `/products/` en **plural**; existe `/products.json` |

**Regla para `_is_product_url()`:** hacer el filtro lo más estricto posible.
DDG devuelve páginas de categoría, marca y blog mezcladas con productos.
Cada URL que pasa el filtro cuesta una navegación Playwright (lenta y costosa).
- PrestaShop: `re.match(r'^\d+-.+\.html$', last_segment)` — probado en menforsan.com
- WooCommerce: comprobar que el path tiene el prefijo correcto Y no es categoría/tag/blog

**Shopify → NO uses DDG, descarga el catálogo entero con `products.json`.** Si la URL
de producto lleva `/products/` (plural), es Shopify y expone un endpoint público
paginado: `GET {dominio}/products.json?limit=250&page=N` (hasta vaciarse). Devuelve
por producto: `handle`, `title`, `body_html`, `images[].src`, `variants[]` (con `sku`,
**sin `barcode`** en el endpoint público), `product_type`, `tags`. Ventajas vs DDG:
un solo fetch trae todo el catálogo, el matching es local (sin coste por producto) y
se compara contra el **título real** (no contra un snippet de DDG). Para multi-mercado
(Shopify Markets) los `handle` se comparten entre mercados → se construye la URL del
mercado deseado como `{dominio}/{mercado}/products/{handle}/`. Implementado en
`marcas/applaws.py` (`_build_shopify_catalog` + `_match_shopify_local`).

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
| Applaws | Applaws | Imágenes ✅ — ES URL test ✅ (url_fabricante) — UK `url_fabricante_2` **corregidos a mano en Shopify y verificados** → ejecutar `Snapshot URLs fabricante` para fijarlos en el repo (registro + caché sembrada) — **pendiente** lote ES (url_fabricante) | web_oficial → marcas/applaws.py (ES: applaws.pet / UK: applaws.com/uk/ vía handle de búsqueda) |
| CALIBRA | CALIBRA | Imágenes ✅ — **URLs**: campo 1 (.es) run #15 corregido a mano; reconocimiento por imagen (CLIP) añadido → re-lanzar con `usar_imagen=true`; campo 2 (.eu) pendiente | shopify_backup (imgs) + web_oficial (URLs) → marcas/calibra.py |
| Acana | Acana | En proceso | web_oficial → marcas/acana.py |
| ARTERO | ARTERO | **Listo para proceso masivo** — scraper testado OK | web_oficial → marcas/artero.py (artero.com/es/petcare/) |
| AFFINITY (ADVANCE, ADVANCE VET, LIBRA, BREKKIES, NATURAL TRAINER, NATURE'S VARIETY) | AFFINITY | Completado — backup OK, listo para optimizar imágenes | web_y_amazon → marcas/affinity.py |
| Virbac | Virbac | **URLs pendiente** — scrapers listos (`marcas/virbac.py`) | web_oficial → marcas/virbac.py (store.es.virbac.com + vet-es.virbac.com) |
| Churu | Churu | **URLs pendiente** — scraper listo (`marcas/churu.py`) | web_oficial → marcas/churu.py (inabafoods-europe.com ES + inabafoods.com US) |
| Farmina ND | Farmina | **URLs pendiente** — scraper listo (`marcas/farmina.py`) | web_oficial → marcas/farmina.py (farmina.com/es) |
| Farmina Vet Life | Farmina Vet Life | **URLs pendiente** — shim listo (`marcas/farmina_vet_life.py`) | web_oficial → marcas/farmina.py compartido |
| Lenda | Lenda | Pendiente (mixto) | shopify_backup + web_oficial |
| Beaphar | BEAPHAR | **Completado** — 126/126 productos con imágenes oficiales; unificaciones B, C, E, L, M aplicadas vía API | web_oficial → marcas/beaphar.py (beaphar.es) |
| Menforsan | MENFORSAN | **En proceso** — fuente `web_y_amazon` probada, fixes aplicados, pendiente proceso masivo | web_y_amazon → marcas/menforsan.py (menforsan.com) |

### Applaws — notas de estrategia (URLs fabricante)

**Objetivo:** poblar los metacampos de URL de cada producto Applaws:
- `fuentes.url_fabricante` ← web ES (`applaws.pet/producto/{slug}/`)
- `fuentes.url_fabricante_2` ← web UK (`applaws.com/uk/products/{handle}/`)

**Los dos sitios usan CMS distintos** → estrategias distintas en `marcas/applaws.py`:

| Sitio | `web_url` | CMS | Idioma | Catálogo caché | Threshold |
|---|---|---|---|---|---|
| ES | `https://applaws.pet/` | WooCommerce | es | `resultados/applaws_catalog.json` | 0.30 |
| UK | `https://applaws.com/uk/` | **Shopify** | en | `resultados/applaws_uk_catalog.json` | 0.22 |

- **ES (WooCommerce, bajo demanda):** slug directo desde el título → fallback DDG
  `site:applaws.pet/producto/` + ranking del h1. URL `/producto/{slug}/` (slug con peso).
- **UK (Shopify, catálogo completo):** se detecta por la URL `/uk/products/{handle}/`
  (plural = Shopify). Se descarga el **catálogo entero de una vez** vía el endpoint
  público `products.json` (`applaws.com/products.json?limit=250&page=N`) — títulos,
  handles, imágenes, `body_html`, SKUs — y el matching se hace **localmente** contra
  todos los títulos reales. **No usa DDG** (salvo fallback si products.json falla).

**Por qué products.json y no DDG en UK:** la primera versión usaba DDG por producto con
título traducido y **falló en 17/17** — la traducción ES→EN es demasiado lossy
(`ATUN Y CANGREJO EN CALDO` → la web es `Tuna Fillet with Crab in Broth Wet Cat Food`,
con "fillet"/"wet cat food" que no están en el título ES). Con el catálogo completo se
compara contra el título inglés real y se elige el de mayor Jaccard, mucho más fiable.

**Matching UK (en `_match_shopify_local`):**
1. **EAN/SKU exacto** (idioma-independiente) → score 1.0. (Nota: el `products.json`
   público de Shopify **no** expone `barcode`, sí `sku`; se prueban ambos por si acaso.)
2. **Jaccard del título traducido** (`_ES_EN`, ~95 términos) vs título inglés. Los tokens
   numéricos/peso (`12x70`, `70`, `2`) se descartan en UK porque el peso es una **variante**
   del producto, no parte del título/handle. Se loguean los 3 mejores candidatos.
- Ejemplo "APPLAWS CAT SOBRE ATUN Y CANGREJO EN CALDO 12X70GR" → tokens EN
  `{broth, cat, crab, pouch, tuna}` vs "Tuna Fillet with Crab in Broth Wet Cat Food":
  score **0.50** → handle `tuna-fillet-with-crab-in-broth-wet-cat-food` ✅
  (distingue de las variantes prawn / jelly, que puntúan menos).
- `_fetch_json` usa el `APIRequestContext` del navegador ya calentado (cookies/UA del
  contexto) para esquivar el filtro de bots del CDN; fallback a navegación + innerText.

**Metacampo destino:** usar el parámetro `url_key` del workflow:
- `url_fabricante` → campo 1 (ES)
- `url_fabricante_2` → campo 2 (UK) — **nunca sobreescribe el campo 1**

**Estado ES — ✅ TEST PASADO:**
- Producto 15509633204611 ("APPLAWS CAT SOBRE PECHUGA DE POLLO Y SALMON 12X70GR"):
  score=1.00, guardado en `fuentes.url_fabricante` → `https://applaws.pet/producto/applaws-cat-sobre-pechuga-de-pollo-y-salmon-12x70gr/`
- **Próximo paso ES:** lanzar lote completo (vendor=Applaws, web_url=https://applaws.pet/,
  url_key=url_fabricante, product_ids vacío = todos los ~80 productos).

**⚠️ Problema detectado en Actions: applaws.com bloquea IPs de datacenter.**
- 1er intento (DDG por producto): **0/80** — traducción demasiado lossy + navegación 403.
- 2º intento (products.json): **HTTP 403** (`products.json pág 1: vacío / HTTP 403`).
  applaws.com filtra las IPs de los runners de GitHub (datacenter), igual que el
  sandbox local. El fallback DDG también dio 0/80 porque cada navegación recibe 403.

**Hallazgo (run 14:48):** el navegador **headed bajo xvfb SÍ pasa Cloudflare** en la home
(`[warm-up] HTTP 200, cf_clearance=sí`), PERO `sitemap.xml` y `products.json` seguían dando
**403 "Just a moment…"**. Causa: el `APIRequestContext` (`page.context.request.get`) tiene
**otro fingerprint** que el navegador, así que Cloudflare no acepta la cookie `cf_clearance`
emitida al navegador y lo vuelve a retar.

**Fix (en `main`): `fetch()` DENTRO del contexto JS de la página** (`_PAGE_FETCH_JS` vía
`page.evaluate`). Es una petición **same-origin** (la página ya está en `applaws.com/uk/`
tras el warm-up) → usa la cookie `cf_clearance` y el fingerprint del navegador que YA
superó el reto. Es exactamente cómo el propio storefront carga sus datos por AJAX.
`_fetch_raw`: método 1 = `fetch()` en la página; método 2 (fallback) = `APIRequestContext`.

**Estrategia completa (cascada):**
1. **`fetch()` in-page** del **sitemap de productos** (`/sitemap_products_*.xml`):
   handle + título (`<image:title>`) + imagen, sin API. Parser `_parse_product_sitemap`.
2. **`fetch()` in-page** de **products.json** (añade `body_html`/SKUs) como complemento.
3. **Navegador headed bajo xvfb** (`APPLAWS_HEADED=1` + `xvfb-run`): imprescindible —
   Chromium headless es bloqueado desde IPs de datacenter; headed obtiene `cf_clearance`.
4. **Diagnóstico** en cada 403: `[bloqueo]/[fetch] HTTP … server/cf/body` en el log.

**Conclusión: Cloudflare bloquea TODO desde datacenter** (sitemap, products.json y
páginas de producto → 403, incluso el `fetch()` in-page). El catálogo vía navegador
**no es viable** desde Actions. La home pasa (da `cf_clearance`) pero no sirve para
los recursos. (La técnica `fetch()` in-page queda documentada por si otra marca con
Cloudflare menos agresivo la necesita.)

**✅ SOLUCIÓN UK QUE FUNCIONA — resolver por HANDLE de la búsqueda (sin navegar):**
La búsqueda multi-motor (`_ddg_find_product_urls`) SÍ devuelve las URLs
`applaws.com/uk/products/{handle}/` correctas; el handle ya viene en la URL, así que
**no hace falta abrir la página** (que es lo que Cloudflare bloquea). `_resolve_uk_via_search`
puntúa el handle (traducido ES→EN) vs el título, quita el sufijo `-2/-3/-4` de Shopify y
prefiere el handle canónico. `find_best_match` UK: catálogo local (si se pudiera) →
si no, handle de búsqueda. **No navega candidatos** (antes hacía 6×80 navegaciones 403).

**Estado UK — ✅ TEST PASADO (run 15:11):**
- Producto 15509633859971 "APPLAWS CAT TARRINA PECHUGA POLLO Y PATO 10X60GR" →
  tokens EN `cat pot breast chicken duck` → `chicken-breast-with-duck-in-broth`
  (score 0.50, eligió el canónico entre 3 empatados), guardado en `url_fabricante_2` ✅.
- **Próximo paso UK:** lote completo (`vendor=Applaws`, `web_url=https://applaws.com/uk/`,
  `url_key=url_fabricante_2`, `product_ids` vacío = todos). Revisar `sin_match` en el
  resumen (productos que la búsqueda no indexa bien, p. ej. arena "Natures Calling") y
  reintentarlos a mano o con un product_id concreto.
- **Importante:** en `product_ids` va el **ID de Shopify** (número grande tipo
  `15509633859971`), NO el EAN. Si se pasa un EAN da 404 (ahora se salta, no aborta).

**1er lote UK completo (80 productos) — 2 bugs detectados y corregidos:**

El primer lote masivo (`url_fabricante_2`) dio 68 "guardadas" / 12 sin_match,
pero **muchas URLs estaban repetidas/erróneas** (p. ej. `ocean-fish-with-salmon`
asignada a 12 productos). Dos causas distintas, ambas corregidas en `main`:

| Bug | Síntoma | Causa | Fix |
|---|---|---|---|
| **Cache-pollution** | 28 productos heredaban la URL de otro ya resuelto | `find_best_match` guarda cada resolución en el dict `catalog` con clave = título ES y SIN `handle`. Para el siguiente producto, `_match_shopify_local` hacía Jaccard **contra esas entradas cacheadas** (no contra un catálogo real) → falso positivo (POLLO ~0.29 vs PESCADO Y SALMON, supera 0.22). | `_is_catalog_entry()`/`_has_real_catalog()` separan catálogo Shopify REAL (entradas con `handle`) de la caché de resoluciones. `_match_shopify_local` **ignora** la caché; `find_best_match` solo hace matching difuso si `_has_real_catalog()`. Si no, cada producto resuelve su propia URL por búsqueda. |
| **Especie perro/gato** | Los **6 productos DOG** recibieron URL de gato (`chicken-...-cat-food`) | applaws.com es gato-first; el scoring solo miraba el sabor (pollo, cordero) e ignoraba la especie. KITTEN caía en productos adultos. | `_species_ok()`: un título DOG solo casa con handles con marca `dog` explícita; gato/sin-especie nunca casa con handle de perro. `_stage_penalty()` x0.5 para kitten↔adulto. Aplicado en `_resolve_uk_via_search` y `_match_shopify_local`. |

**Caché contaminada borrada:** `resultados/applaws_uk_catalog.json` (40 entradas del
run con bug) se eliminó del repo. Como `find_best_match` hace **cache-hit exacto por
título antes** de resolver, esas URLs erróneas se reutilizarían sin pasar por los
guards nuevos. Al borrarla, el re-run resuelve cada producto desde cero.

**⚠️ Metacampos ya escritos en Shopify:** el run con bug dejó URLs erróneas en
`url_fabricante_2` de ~34 productos (28 cache-pollution + 6 DOG). El re-run las
**sobrescribe** si las resuelve bien; pero si un producto pasa a `sin_match`, la
URL errónea **permanece**. Tras el re-run, revisar manualmente los `sin_match`
(sobre todo los 6 DOG y los KITTEN) y limpiar su `url_fabricante_2` si quedó mal.

**Limitaciones conocidas (sin catálogo UK por Cloudflare):**
- **Formato (lata/sobre/tarrina) del mismo sabor** → mismo handle UK. El sabor es
  correcto pero la variante de formato exacta puede diferir (UK los vende como
  productos distintos). Sin el catálogo completo no se distinguen por búsqueda.
- **Variante "plain" vs con ingrediente extra** (p. ej. `FILETE DE ATUN` → quedó
  `tuna-fillet-with-crab`): depende de que DDG devuelva el handle "plain" entre los
  candidatos. Revisar a mano si el sabor base no encaja.

**Re-lanzar el lote UK** (mismos parámetros): `vendor=Applaws`,
`web_url=https://applaws.com/uk/`, `url_key=url_fabricante_2`, `product_ids` vacío,
`rebuild_catalog=false` (la caché ya está borrada → resolución limpia).

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

**URLs fabricante (web → metacampos):** `marcas/calibra.py` adaptado para resolver
**por dominio** según `web_url` (antes fusionaba todas las fuentes). Cada producto
de Calibra cuelga de la raíz con slug `calibra-…` (`/{slug}`), p. ej.
`mycalibra.es/calibra-dog-life-adult-lamb-400g`. `scrape_catalog(web_url)` elige la
fuente de `SOURCES` cuyo dominio coincide y cachea **por sitio**
(`resultados/calibra_{es|eu|cat}_catalog.json`); `find_best_match` devuelve la URL
de ese sitio. Así el workflow `Resolver URLs fabricante` se corre **dos veces**:

| Run | `web_url` | `url_key` | Idioma |
|---|---|---|---|
| Campo 1 | `https://www.mycalibra.es/` | `url_fabricante` | español |
| Campo 2 | `https://www.mycalibra.eu/` | `url_fabricante_2` | inglés (misma estructura `/{slug}`) |

- **`calibra.cat` NO sirve como campo 2**: es portal de marca, no expone páginas
  `/{slug}` (verificado por búsqueda). El catálogo inglés con la misma estructura que
  mycalibra.es está en **`mycalibra.eu`** → usar esa como campo 2. El código soporta
  los tres dominios por si calibra.cat cambia.
- **Anti-bot**: la home devuelve un gate ("Ověření přístupu", Calibra es checa). El
  scraper trae contexto endurecido (Sec-Fetch, bypass `navigator.webdriver`, warm-up
  de la home) y corre **headed bajo xvfb** cuando el workflow exporta `APPLAWS_HEADED=1`
  (el de Resolver URLs ya lo hace). Si aun así da gate desde Actions, plan B = DDG
  `site:mycalibra.es` por producto + resolver la URL del resultado sin navegar.
- **Test**: 1 `product_id` primero (hace el crawl completo del sitio y cachea), verificar
  `fuentes.url_fabricante` en el admin, luego lote con `product_ids` vacío (reusa caché).
  Matching ES↔EN ya cubierto por `_SYNONYMS`; el peso (400g/2kg) se ignora en `IGNORE_TOKENS`.

**Análisis del run #15 (campo 1, mycalibra.es) — 146/146 "guardadas" pero 22 erróneas:**
El resolver aceptaba **cualquier** match con `score ≥ 0.12` (`MIN_SCORE`), así que
**forzaba una URL para todos** aunque el producto no existiera en mycalibra.es.
Resultado real: 74 alta confianza (≥0.70), 50 media, **22 basura (score 0.12-0.45)**.
Tres causas, todas por usar **solo texto**:
1. **Producto ausente en .es** → forzado a página genérica. Los 9 ROCKETS (roedores)
   cayeron a `calibra-rockets` (0.14-0.33). Existen en **.eu** (campo 2), no en .es.
2. **Confusión de snack** → 6 JOY DOG CHEWY ("huesos") → `dental-brushes`/`salmon-sticks` (0.29-0.38).
3. **Error de especie** → producto 108 `DOG6 PREMIUM LATA` → `cat-premium-...-100g` (URL de **gato**, 0.20).
Tabla completa en `resultados/calibra_run15_resultado.csv` (columna `sospechoso`).

### Reconocimiento por imagen (Google Lens) en el resolver — `core/image_match.py`

Segundo eje de matching junto al texto: la foto del producto **Shopify** se compara
con la foto del candidato del **fabricante**. Confirma matches correctos (aunque el
texto sea flojo) y **RECHAZA** los falsos positivos forzados (→ `sin_match` en vez de
una URL errónea). Activación: input `usar_imagen=true` del workflow Resolver URLs.

**Dos backends (auto-detect, degradación segura):**
- `clip` — embeddings CLIP ViT-B-32 (`open_clip`, `requirements-vision.txt`). Reconoce
  el **mismo producto aunque la foto sea distinta** (ángulo/fondo/recorte) = Google Lens.
- `hash` — hash perceptual multi-algoritmo (average+diff+DCT), **sin dependencias**.
  Solo detecta la **misma foto** reutilizada; si CLIP no está instalado, se usa este.

**Umbrales por backend** `THRESHOLDS=(STRONG, WEAK)`: clip `(0.82,0.62)`, hash `(0.86,0.72)`.
`sim≥STRONG`→mismo producto; `sim≤WEAK`→productos distintos.

**Cómo se evita el 403 del CDN de imágenes:** el CDN de mycalibra da **403 sin navegador**
(igual que el HTML). Por eso la huella visual de cada producto del catálogo se
**precomputa DURANTE el scrape** (navegador vivo, ya pasó el gate) vía
`page.context.request.get` y se guarda en el catálogo (`entry["img_feat"]`, serializable
en JSON). En match-time solo se descarga la imagen de Shopify (CDN **público**) y se
compara contra las huellas precomputadas (matemática de vectores, sin red).

**Lógica de decisión (`find_best_match`, pesos `_W_TEXT=0.45 / _W_IMG=0.55`):**
1. **Confirmación visual fuerte** (`img≥STRONG`, foto idéntica/igual) → acepta el candidato
   aunque el texto sea bajo. Vale para **ambos** backends. Es el caso más fiable.
2. **CLIP**: `combinado = 0.45·texto + 0.55·img`. Si `img≤WEAK y texto<0.55` → **RECHAZO**
   (sin_match). Si `combinado≥0.45` → acepta. Discrimina especie/formato/snack.
3. **hash** (fallback): sin confirmación fuerte la imagen no discrimina → decide el texto
   con umbral elevado `_TEXT_ONLY_MIN=0.30` (nunca peor que antes; mata la basura <0.30).

**Plumbing genérico:** `core/process_brand.py` (`run_resolve_urls`) pasa
`product_images` (los `images[].src` de Shopify) a `find_best_match` **si el scraper
acepta ese parámetro** (vía `inspect.signature`, igual que `barcode`). Hoy lo implementa
CALIBRA; otras marcas lo adoptan añadiendo el parámetro. Con `IMAGE_MATCH=0` (o
`usar_imagen=false`) todo el camino visual se desactiva (cero torch, solo texto).

**Modo DRY-RUN (validar sin tocar Shopify):** input `dry_run=true` (o `--dry-run`).
El resolver calcula qué URL pondría pero **no escribe** en Shopify; guarda
`resultados/{slug}_resolver_dryrun.json` y, si existe `{slug}_urls_snapshot.json`,
**compara** automáticamente (igual / distinto-REGRESIÓN / borraría / aporta) e imprime
el % de reproducción. Imprescindible antes de re-pasar el resolver por encima de un
`url_fabricante` ya corregido a mano: mide la precisión sin riesgo de perder el trabajo.

**Comportamiento de escritura (importante):** `_save_source_url` **sobrescribe** el
metacampo `url_key` y **añade** al histórico (`fuentes.historico` es **append-only**,
nunca se borra). `_clear_source_url` (con `clear_on_no_match=true` y sin_match) **borra**
el metacampo `url_key` pero **no** toca el histórico. ⚠️ Re-correr el resolver sobre un
campo ya correcto solo puede empeorarlo → usar `dry_run` primero. El backup del trabajo
manual es el **Snapshot** (`{slug}_urls_snapshot.json`), no el histórico.

**Análisis run #15 (texto-solo) vs snapshot corregido — 79% (107/134), 26 regresiones:**
Diagnóstico con dos causas distintas (el dry-run las separa):
- **(A) 16 — la página correcta SÍ está en el catálogo** (confusión de formato/línea/especie:
  LATA vs seco, premium-100g vs life-200g, DOG6 PREMIUM→URL de gato). → **lo arregla la imagen (CLIP)**.
- **(B) 10 — la página correcta FALTA del catálogo** (el crawl solo veía 3 categorías):
  9 ROCKETS (`rockets-mix/sticks-*`, roedores) + 1 `verve-semi-moist-herring`. La imagen
  **no** puede casar lo que no está → **arreglado con descubrimiento por sitemap**.

**Descubrimiento por sitemap** (`_collect_sitemap_links` en `marcas/calibra.py`): además
del crawl de categorías, `_scrape_source` lee `{base}/sitemap.xml` (sigue índices anidados)
vía el navegador y añade toda URL `/{slug}` que empiece por `calibra-`. Captura los ROCKETS
y snacks no enlazados. Requiere `rebuild_catalog=true` para reconstruir con el sitemap.

**Re-lanzar CALIBRA — primero DRY-RUN** (campo 1, mide cuánto reproduce de las 134):
`vendor=CALIBRA`, `web_url=https://www.mycalibra.es/`, `url_key=url_fabricante`,
`usar_imagen=true`, **`rebuild_catalog=true`** (reconstruye con sitemap + huellas),
`clear_on_no_match=false`, **`dry_run=true`**. Revisar el bloque "DRY-RUN vs SNAPSHOT"
del log: si reproduce ~134/134, lanzar el real (`dry_run=false`); si quedan regresiones,
afinar antes. Campo 2 (`mycalibra.eu`, `url_fabricante_2`) igual con `dry_run=true` primero
(pero allí el snapshot de campo 2 está casi vacío → sirve más para revisar a ojo).

### Farmina — notas de estrategia (URLs fabricante)

**Dos submarcas, un dominio:** `farmina.com/es/eshop/`
- Vendor **"Farmina"** (N&D Natural & Delicious) → `marcas/farmina.py`
- Vendor **"Farmina Vet Life"** → `marcas/farmina_vet_life.py` (shim que re-exporta `farmina.py`)

**CMS:** PrestaShop — URLs `/es/eshop/{categoria}/{subcategoria}/{id}-{slug}.html`.
El `id` numérico NO es derivable del título → solo DDG (igual que beaphar/menforsan).

**Problema clave — peso en el título Shopify:**
Farmina tiene una página por receta (p. ej. "Bacalao, Calabaza y Melón Puppy Mini"),
pero Shopify tiene un producto por tamaño de saco (2.5 KG, 7 KG, 12 KG de la misma receta).
El scraper elimina el peso del título antes del matching y usa la misma clave de caché para
todos los tamaños → muchos-a-uno, todos apuntan a la misma URL.

**Estrategia de matching:**
- `_clean_for_match(title)`: quita prefijo ("FARMINA ND ", "VET LIFE ", "N&D ", "ND ") y peso.
- DDG: `site:farmina.com/es/eshop/ {título limpio}` → candidatos con `_is_product_url()`.
- Score Jaccard (con stemming ES) entre tokens del título limpio y h1 de la página.
- `MATCH_THRESHOLD = 0.25` (tolera que el h1 web no tenga los tokens de línea/ocean/prime).

**Caché compartida:** `resultados/farmina_catalog.json` — sirve para ambos vendors.
Ejecuciones anteriores de N&D precargan recetas que Vet Life (y viceversa) ya no necesitan buscar.

**Parámetros workflow `Resolver URLs fabricante`:**

| Campo | Vendor N&D | Vendor Vet Life |
|---|---|---|
| `vendor` | `Farmina` | `Farmina Vet Life` |
| `web_url` | `https://www.farmina.com/es/` | `https://www.farmina.com/es/` |
| `url_key` | `url_fabricante` | `url_fabricante` |
| `rebuild_catalog` | `false` | `false` |
| `clear_on_no_match` | `false` | `false` |

Solo existe un campo URL para Farmina (no hay segunda web en otro idioma).
Lanzar N&D primero para poblar la caché, luego Vet Life (reutiliza caché).

### CHURU — notas de estrategia (URLs fabricante)

**Dos sitios, un scraper:** `marcas/churu.py` enruta por `web_url`
- `inabafoods-europe.com` → campo 1 (español, `churu_es_catalog.json`)
- `inabafoods.com` → campo 2 (inglés, `churu_us_catalog.json`)

**CMS:** Custom PHP (Europa) — URLs `/es/shop/item.php?it_id={numeric_id}` (query param, no path).
El `it_id` numérico NO es derivable del título → DDG bajo demanda.
El sitio US NO es Shopify (products.json devuelve 403).

**Anti-bot:** Ambos bloquean directamente (HTTP 403) → Playwright necesario.
El campo `catalog['_site']` ("es" | "us") lleva la info de sitio a `find_best_match`.

**Matching US (campo 2):** `_tokenize(title, translate_en=True)` aplica `_ES_EN`
(diccionario de ~20 sabores atun→tuna, pollo→chicken, gambas→shrimp…) antes de Jaccard.
`MATCH_THRESHOLD=0.20` (títulos CHURU cortos, pocos tokens significativos).

**Parámetros workflow `Resolver URLs fabricante`:**

| Run | `vendor` | `web_url` | `url_key` |
|---|---|---|---|
| Campo 1 | `Churu` | `https://www.inabafoods-europe.com/` | `url_fabricante` |
| Campo 2 | `Churu` | `https://inabafoods.com/` | `url_fabricante_2` |

### Virbac — notas de estrategia (URLs fabricante)

**Dos sitios, un scraper:** `marcas/virbac.py` enruta por `web_url`
- `store.es.virbac.com` → campo 1 (tienda WooCommerce, nombres consumidor)
- `vet-es.virbac.com` → campo 2 (portal veterinario Liferay, acceso público sin login)

**store.es.virbac.com:** WooCommerce, 403 directa → Playwright. URL de producto: probablemente
`/{animal}/{categoria}/{slug}/` (patrón jerárquico de la taxonomía WooCommerce del sitio).
Filtro `_is_product_url`: acepta rutas con ≥2 segmentos, rechaza known non-products.

**vet-es.virbac.com:** Liferay, URL de producto: `/home/productos/{animal}/{cat}/{slug}.html`.
Filtro estricto: require `/home/productos/` + `.html` al final. No requiere login.
`catalog['_site']` = "vet" o "store".

**Parámetros workflow `Resolver URLs fabricante`:**

| Run | `vendor` | `web_url` | `url_key` |
|---|---|---|---|
| Campo 1 | `Virbac` | `https://store.es.virbac.com/` | `url_fabricante` |
| Campo 2 | `Virbac` | `https://vet-es.virbac.com/` | `url_fabricante_2` |

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
