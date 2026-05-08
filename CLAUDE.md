# Shopify Product Images — Guía de trabajo

Repositorio para actualizar las imágenes de productos Shopify con las imágenes oficiales
descargadas directamente del sitio web de cada marca.

Tienda: `7ev1zx-eg.myshopify.com`

---

## Flujo de trabajo por marca

### 1. Análisis previo
Antes de procesar, crear un script `analizar_<marca>.py` que:
- Obtiene todos los productos del vendor en Shopify
- Scrapea el catálogo del sitio web oficial de la marca
- Ejecuta el algoritmo de matching e imprime los resultados
- **No modifica nada en Shopify** — solo lectura

Workflow asociado: `.github/workflows/analizar_<marca>.yml`

### 2. Procesador principal
Script `process_<marca>.py` con estos argumentos:
- Sin args: procesa todos los productos del vendor
- `--product-id ID`: procesa un solo producto (modo prueba)
- `--only-ids ID1,ID2,...` o variable de entorno `PRODUCT_IDS`: procesa una lista específica de IDs
- `--rebuild-catalog`: borra la caché y re-scrapea el sitio de la marca

**Lo que hace por cada producto:**
1. Busca el mejor match en el catálogo de la marca (algoritmo de scoring por tokens)
2. Descarga la imagen oficial
3. Procesa: 2000×2000 px, JPEG calidad 85, fondo blanco, 5% de padding en cada lado
4. Elimina todas las imágenes antiguas del producto en Shopify
5. Sube la nueva imagen como posición 1

**Caché del catálogo:** se guarda en `resultados/<marca>_catalog.json` para no re-scrapear
en cada ejecución. Usar `--rebuild-catalog` solo si el catálogo de la marca cambió.

### 3. Workflows de GitHub Actions

| Workflow | Descripción |
|---|---|
| `analizar_<marca>.yml` | Solo análisis, sin cambios en Shopify |
| `test_<marca>.yml` | Prueba con un producto concreto (`--product-id`) |
| `procesar_todos_<marca>.yml` | Procesado masivo de todos los productos |
| `auditar_<marca>.yml` | Auditoría post-procesado para detectar errores |
| `reprocess_corregidos_<marca>.yml` | Re-proceso de IDs concretos con correcciones |

Todos usan `workflow_dispatch` (ejecución manual).

**Importante:** los workflows deben hacer checkout de la **rama por defecto** del repo
para que aparezcan en la pestaña de Actions de GitHub.

### 4. Auditoría y corrección
Tras el procesado masivo, ejecutar `auditar_<marca>.py` que:
- Compara el match usado en el procesado vs. el match correcto según el algoritmo actualizado
- Genera `resultados/auditoria_<marca>.csv` con columnas:
  `shopify_id, shopify_title, match_usado, match_correcto, cambio`
- Los productos con `cambio=SI` son los que necesitan re-proceso

Para corregir los productos con match incorrecto:
1. Anotar sus IDs
2. Crear `reprocess_corregidos_<marca>.yml` con `PRODUCT_IDS` fijados en el workflow
3. El script los recupera uno a uno con `get_product()` y los re-procesa

---

## Autenticación Shopify

```
POST https://{SHOP_DOMAIN}/admin/oauth/access_token
  grant_type=client_credentials
  client_id=CLIENT_ID
  client_secret=CLIENT_SECRET
```

Las credenciales se pasan como variables de entorno en los workflows (no se almacenan en el repo).
Variables: `SHOP_DOMAIN`, `CLIENT_ID`, `CLIENT_SECRET`.

---

## Algoritmo de matching

El scoring compara tokens del título Shopify con tokens del nombre/slug del producto en
el sitio de la marca. Pasos clave:

1. Normalizar: quitar acentos, minúsculas, eliminar stopwords y tokens ignorados
   (pesos, unidades, nombre de la marca, etc.)
2. Traducir términos ES→EN (p. ej. "cerdo"→"pork", "pescado"→"fish", "caja"→"humedo")
3. Separar húmedos de secos: si el título contiene CAJA/TARRO/HUMEDO → buscar en húmedos
4. Si contiene TREAT → filtrar solo entre treats
5. Score = intersección / unión de tokens (Jaccard)
6. Umbral mínimo: 0.10 — por debajo se descarta el match

---

## Estructura de resultados

```
resultados/
  <marca>_catalog.json          # caché del catálogo scrapeado
  proceso_completo_<marca>.txt  # log del procesado masivo
  auditoria_<marca>.csv         # comparativa match usado vs. correcto
  reprocess_corregidos_<marca>.txt  # log del re-proceso de correcciones
```

---

## Marcas completadas

| Marca | Vendor Shopify | Estado | Productos |
|---|---|---|---|
| Farmina N&D | Farmina | Completado | 157 |
| Farmina Vet Life | Farmina Vet Life | Completado | ~157 (28 re-procesados) |

---

## Añadir una nueva marca

1. Identificar el vendor exacto en Shopify y la URL del catálogo en su web oficial
2. Copiar `analizar_farmina_vet_life.py` como base para `analizar_<marca>.py`
3. Adaptar las URLs de categorías, el parsing HTML y el preprocesado de nombres
4. Ejecutar análisis → revisar matches → ajustar algoritmo hasta que sean correctos
5. Ejecutar `test_<marca>.yml` con un producto de prueba
6. Ejecutar `procesar_todos_<marca>.yml`
7. Ejecutar auditoría → si hay errores, crear `reprocess_corregidos_<marca>.yml`
