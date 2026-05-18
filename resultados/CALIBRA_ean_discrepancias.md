# EAN CALIBRA con discrepancia entre Shopify y Distrivet

**Fecha:** 18/05/2026  
**Método:** descarga XML crudo Distrivet (8 791 refs) + matching manual/automático por nombre y tamaño.  

---

## Contexto: ¿por qué hay discrepancias?

CALIBRA ha renovado los EANs de la línea **JOY Classic** pasando del prefijo `8594062` al `8595706`.
Shopify conserva los EANs antiguos. Distrivet ya trabaja con los nuevos.
Además, algunos productos han sido **renombrados** (LARGE FILLET→BITS, AROS→BITS)
y otros **descatalogados** (Joy Mini, Joy CAT, SUSHI, SLICE…).

---

## 🔴 Cambios de EAN confirmados — actualizar en Shopify (9)

Mismo producto, EAN renovado. Actualizar el barcode en Shopify con el EAN de Distrivet.

| EAN actual Shopify | EAN correcto (Distrivet) | Producto | Stock Distrivet |
|-------------------|--------------------------|----------|:---------------:|
| `8595706703536` | `8595706703581` | CALIBRA DOG LIFE POUCH PUPPY&JUNIOR CHICKEN 10X150G | 31 · 11,59€ |
| `8594062084952` | `8595706703932` | CALIBRA JOY DOG CLASSIC STRIPS CORDERO 80GR | 0 · 1,9€ |
| `8594062084983` | `8595706703963` | CALIBRA JOY DOG CLASSIC STICKS SALMON 80GR | 0 · 1,7€ |
| `8594062087687` | `8595706703857` | CALIBRA JOY DOG CLASSIC STICKS TERNERA 80GR | 0 · 1,7€ |
| `8594062088493` | `8595706703949` | CALIBRA JOY DOG CLASSIC CORDERO STRIPS 250GR | 0 · 5,55€ |
| `8594062088479` | `8595706703895` | CALIBRA JOY DOG CLASSIC PATO STRIPS 250GR | 0 · 5,35€ |
| `8594062088486` | `8595706703918` | CALIBRA JOY DOG CLASSIC PESCADO POLLO SANDWICH 250GR | 0 · 4,95€ |
| `8594062088509` | `8595706703970` | CALIBRA JOY DOG CLASSIC SALMON STICKS 250GR | 0 · 4,75€ |
| `8594062088455` | `8595706703864` | CALIBRA JOY DOG CLASSIC TERNERA STICKS 250GR | 0 · 4,75€ |

## ⚠ Error de EAN en Distrivet (Shopify correcto)

| EAN correcto (Shopify) | EAN truncado en Distrivet | Producto | Stock Distrivet |
|------------------------|---------------------------|----------|:---------------:|
| `8595706701808` | `595706701808` *(falta el '8' inicial)* | CALIBRA DOG6 PREMIUM CON POLLO Y VACUNO LATA 12X1240G | 50+ · 25,94€ |

## 🟠 Productos renombrados — verificar manualmente (3)

El nombre cambió entre versiones (ej. LARGE FILLET → BITS). Confirmar físicamente que es el mismo producto antes de actualizar.

| EAN Shopify | EAN Distrivet | Producto Shopify | Nombre Distrivet | Stock |
|-------------|---------------|-----------------|-----------------|:-----:|
| `8594062084969` | `8595706704052` | CALIBRA JOY DOG CLASSIC LARGE FILLET CORDERO 80GR | CALIBRA JOY DOG CLASSIC BITS CORDERO 80G | 50+ |
| | | *LARGE FILLET CORDERO 80G → ahora se llama BITS CORDERO 80G en Distrivet (verificar empaque)* | | |
| `8594062084976` | `8595706704038` | CALIBRA JOY DOG CLASSIC AROS POLLO 80GR | CALIBRA JOY DOG CLASSIC BITS POLLO 80G | 50+ |
| | | *AROS POLLO 80G → ahora se llama BITS POLLO 80G en Distrivet (diferente formato, verificar)* | | |
| `8594062089223` | `8595706704045` | CALIBRA JOY DOG CLASSIC PECHUGA POLLO 250GR | CALIBRA JOY DOG CLASSIC BITS POLLO 250G | 50+ |
| | | *PECHUGA POLLO 250G → BITS POLLO 250G en Distrivet (verificar si es el mismo producto renombrado)* | | |

## 🟡 Coincidencias dudosas — tamaño diferente (2)

El producto más cercano en Distrivet tiene distinto tamaño. Probable descatalogación del formato antiguo.

| EAN Shopify | Producto Shopify | Más cercano en Distrivet | Nota |
|-------------|-----------------|--------------------------|------|
| `8594062084990` | CALIBRA JOY DOG CLASSIC SANDWICH PESCADO POLLO 80GR | `8595706703918` CALIBRA JOY DOG CLASSIC SANDWICH PESCADO Y POLLO 250G | SANDWICH PESCADO POLLO 80GR: en Distrivet solo existe el formato 250G. Tamaño distinto — verificar si fue descatalogado el 80G |
| `8594062084921` | CALIBRA JOY DOG CLASSIC SLICE PESCADO POLLO 80GR | `8595706703918` CALIBRA JOY DOG CLASSIC SANDWICH PESCADO Y POLLO 250G | SLICE PESCADO POLLO 80GR: formato 'SLICE' no existe en Distrivet, el más parecido es SANDWICH 250G — posiblemente descatalogado |

## ⛔ Productos Shopify sin equivalente en Distrivet (13)

No hay ningún EAN ni producto comparable en el catálogo Distrivet.
Evaluar si mantener, descatalogar o buscar proveedor alternativo.

| EAN Shopify | Producto | Motivo |
|-------------|----------|--------|
| `8594062086727` | CALIBRA DOG EXPERT NUTRITION ENERGY 12KG | Distrivet tiene Light, Mobility, Sensitive, Neutered — pero NO Energy |
| `8595706703260` | CALIBRA DOG SNACK ED. LIMITADA CARNE DE VACUNO Y POLLO 80GR | Edición limitada, no distribuida por Distrivet |
| `8595706700627` | CALIBRA DOG VERVE SNACK SEMIHÚMEDO ARENQUE FRESCO 150G | Línea Verve Snack no en Distrivet |
| `8595706700580` | CALIBRA DOG VERVE CRUNCHY SNACK PATO FRESCO 150G | Línea Verve Snack no en Distrivet |
| `8595706700610` | CALIBRA DOG VERVE SNACK SEMIHÚMEDO PATO FRESCO 150G | Línea Verve Snack no en Distrivet |
| `8595706700634` | CALIBRA DOG VERVE SNACK SEMIHÚMEDO POLLO FRESCO 150G | Línea Verve Snack no en Distrivet |
| `8594062084907` | CALIBRA JOY CAT CLASSIC STICKS SALMON 70GR | Joy CAT no aparece en Distrivet (solo Joy DOG) |
| `8594062084891` | CALIBRA JOY CAT CLASSIC STRIPS PESCADO 70GR | Joy CAT no aparece en Distrivet (solo Joy DOG) |
| `8594062085003` | CALIBRA JOY DOG CLASSIC BACALAO POLLO SUSHI 80GR | Formato SUSHI/BACALAO no existe en Distrivet — descatalogado |
| `8594062087250` | CALIBRA JOY DOG CLASSIC DENTAL BRUSHES 85GR | Dental Brushes no aparece en Distrivet |
| `8594062085072` | CALIBRA JOY DOG MINI DADOS SALMON 70GR | Línea JOY MINI no en Distrivet |
| `8594062085058` | CALIBRA JOY DOG MINI SANDWICH BACALAO PATO 70GR | Línea JOY MINI no en Distrivet |
| `8594062085065` | CALIBRA JOY DOG MINI SANDWICH BACALAO POLLO 70GR | Línea JOY MINI no en Distrivet |

---

## Resumen de acciones

| Acción | Cantidad |
|--------|--------:|
| 🔴 Actualizar EAN en Shopify (confianza alta) | 9 |
| ⚠  Sin acción en Shopify — error es de Distrivet | 1 |
| 🟠 Verificar físicamente antes de actualizar | 3 |
| 🟡 Tamaño diferente — evaluar si descatalogado | 2 |
| ⛔ Sin equivalente en Distrivet | 13 |