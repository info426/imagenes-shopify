# Cruce ARTERO — Shopify ↔ Distrivet

Fuentes: export Shopify (`products_export.csv`, 34 productos ARTERO) ×
stock proveedor Distrivet (`distrivet_artero.json`, 52 referencias ARTERO).
Enlace por EAN/barcode. Precio Shopify = PVP; precio Distrivet = coste mayorista.

## Resultado global

- **EANs**: todas las variantes Shopify con EAN coinciden con una referencia Distrivet.
- **6 grupos unificados**: EANs correctos en los 3/2 tamaños o colores. Sin errores de barcode.
- Pendiente: 3 precios anómalos, 2 variantes faltantes, 4 EANs no verificables (parte cortada del XML).

## Verificación grupos unificados

| Grupo | Variantes Shopify | EANs vs Distrivet |
|---|---|---|
| CHAMPU VITALIZANTE | 100ML / 250ML / 5L | ✅ los 3 coinciden |
| CHAMPU HIDRATANTE | 100ML / 300ML / 5L | ✅ EAN; ⚠ precio 300ML |
| CHAMPU BLANC | 100ML / 300ML / 5L | ✅ EAN; ⚠ precio 300ML |
| CHAMPU DETOX CARBON ACTIVO | 100ML / 250ML / 5L | ✅ los 3 coinciden |
| CORREA PELUQUERIA 50CM | Amarillo / Azul / Rosa | ✅ los 3 coinciden |
| CORREA DOG CONTROL 360 | XS / M | ⚠ falta talla S; ⚠ precio M |

## Discrepancias a revisar

### 1. Precios anómalos (margen fuera de patrón x1.5–x2.3)

| Producto | Variante | PVP actual | Coste | Margen | Nota |
|---|---|---|---|---|---|
| CHAMPU HIDRATANTE | 300 ML | 29,00 € | 8,74 € | x3,32 | Sobreprecio — el resto de champús 250-300ML están en 15-19 € |
| CORREA DOG CONTROL 360 | M 25KG | 29,00 € | 9,94 € | x2,92 | Mismo 29,00 € sospechoso (placeholder); XS está a 15,73 € |
| CHAMPU BLANC | 300 ML | 12,39 € | 10,59 € | x1,17 | Infraprecio — solo 17% margen; igual que su 100ML (12,39 €) |

Los dos valores de **29,00 €** idénticos parecen residuos de la unificación destructiva
sin corregir. Sugerido: HIDRATANTE 300ML ≈ 15-17 €, DOG CONTROL M ≈ 21 €.

### 2. Variantes faltantes en Shopify

- **CORREA DOG CONTROL 360 — talla S (15KG)**: existe en el catálogo pero no en Shopify ni en el tramo de Distrivet recibido. Verificar EAN/precio.
- **CORREA PINZA AMARILLA** (Distrivet Y509, EAN 8435037182436, 7,04 €): Shopify solo tiene PINZA ROSA. Falta la amarilla (y posible azul).

### 3. EANs Shopify no verificables

El XML de Distrivet se cortó en Y509; estos 4 EANs no se pudieron contrastar (probablemente sí existen en la parte no recibida):

- ARTERO CORREA PINZA ROSA — 8435037182450
- ARTERO LAZOS VARIADOS 10 UNI — 8435037108788
- ARTERO SAMBA Bayeta — 8435037106968
- ARTERO STATIC CONTROL 150 ML — 8435037165200

### 4. En Distrivet pero no en Shopify (informativo)

- EXPOSITOR PROTEIN Y KERATIN 12UD (expositor, no se vende suelto)
- EXPOSITOR 9 CHAMPÚS DE VIAJE 100ML (expositor)
- PACIFEEL 150ML JARABE (no es ARTERO)
