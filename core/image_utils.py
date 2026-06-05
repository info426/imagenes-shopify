"""
Utilidades de procesamiento de imagen compartidas por todas las marcas.

Pipeline estándar:
  1. Detectar transparencia → fondo blanco o blur-fill según tipo
  2. Detectar fondo blanco (esquinas) → aplicar padding 5% si corresponde
  3. Redimensionar a 2000×2000 centrado sobre canvas blanco
  4. Exportar WebP calidad 90 optimizado para Core Web Vitals
"""

import base64
import logging
from io import BytesIO

from PIL import Image, ImageChops, ImageFilter

log = logging.getLogger(__name__)

TARGET_SIZE    = (2000, 2000)
WEBP_QUALITY   = 80
PADDING        = 0.05
WHITE_THRESH   = 245
WHITE_MIN_FRAC = 0.60
MIN_DIM        = 800    # px mínimo en cualquier dimensión para aceptar imagen web


def autocrop_white(img: Image.Image, thresh: int = 15) -> Image.Image:
    """Elimina márgenes blancos del producto detectando el bounding box del contenido."""
    bg   = Image.new("RGB", img.size, (255, 255, 255))
    diff = ImageChops.difference(img, bg)
    mask = diff.point(lambda x: 0 if x < thresh else 255).convert("L")
    bbox = mask.getbbox()
    if bbox:
        cropped = img.crop(bbox)
        log.info(f"    [autocrop] {img.size} → {cropped.size}")
        return cropped
    return img


def composite_on_white(img: Image.Image) -> Image.Image:
    """Compone una imagen RGBA/P sobre fondo blanco."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba.convert("RGB"), mask=rgba.split()[3])
        return bg
    return img.convert("RGB")


def fill_transparent_with_blur(img_rgba: Image.Image) -> Image.Image:
    """
    Para imágenes mayormente opacas con pequeñas esquinas transparentes
    (ej. banners con bordes redondeados): rellena las esquinas con los
    colores adyacentes difuminados en lugar de blanco puro.
    """
    alpha = img_rgba.split()[3]
    rgb = img_rgba.convert("RGB")
    blurred = rgb.filter(ImageFilter.GaussianBlur(radius=40))
    result = blurred.copy()
    result.paste(rgb, (0, 0), mask=alpha)
    return result


def white_background_ratio(img_rgb: Image.Image) -> float:
    """Ratio de píxeles blancos (≥245,245,245) en los 4 parches de esquina (5%)."""
    w, h = img_rgb.size
    patch = max(20, int(min(w, h) * 0.05))
    step = 2
    total = white = 0
    for x1, y1, x2, y2 in [
        (0, 0, patch, patch),
        (w - patch, 0, w, patch),
        (0, h - patch, patch, h),
        (w - patch, h - patch, w, h),
    ]:
        for x in range(x1, x2, step):
            for y in range(y1, y2, step):
                r, g, b = img_rgb.getpixel((x, y))
                white += int(r >= WHITE_THRESH and g >= WHITE_THRESH and b >= WHITE_THRESH)
                total += 1
    ratio = white / total if total else 0.0
    log.info(f"    [bg] {white}/{total} ({ratio:.0%})")
    return ratio


def is_white_background(img_rgb: Image.Image) -> bool:
    return white_background_ratio(img_rgb) >= WHITE_MIN_FRAC


def process_image(img: Image.Image, force_padding: bool | None = None) -> Image.Image:
    """
    Procesa una imagen al estándar de la tienda:
    - Fondo blanco (transparencia → composite o blur-fill según tipo)
    - Padding 5% si fondo blanco detectado, sin padding si ilustración
    - Canvas 2000×2000 blanco, imagen centrada

    force_padding: None=auto-detectar, True=siempre padding, False=nunca padding
    """
    has_alpha = (img.mode in ("RGBA", "LA") or
                 (img.mode == "P" and "transparency" in img.info))

    if has_alpha:
        rgba = img.convert("RGBA")
        alpha = rgba.split()[3]
        hist = alpha.histogram()
        transparent_ratio = sum(hist[:128]) / (img.width * img.height)
        log.info(f"    [alpha] {transparent_ratio:.0%} transparente")
        if transparent_ratio > 0.15:
            composited = composite_on_white(rgba)
        else:
            composited = fill_transparent_with_blur(rgba)
    else:
        composited = composite_on_white(img)

    white_ratio = white_background_ratio(composited)
    detected_white = white_ratio >= WHITE_MIN_FRAC

    if force_padding is None:
        use_padding = detected_white
    else:
        use_padding = force_padding
        log.info(f"    [bg] forzado: {'padding 5%' if use_padding else 'sin padding'}")

    # Fills-frame detection: fondo blanco pero contenido ocupa >85% del frame
    # (foto lifestyle, ilustración) → sin padding.
    # Excepción: fondo ≥95% blanco con crop mínimo (<3%) = foto de producto
    # recortada al ras (ej. Amazon _AC_SL1500_) → sí necesita padding.
    already_cropped = False
    if use_padding and force_padding is None:
        trial = autocrop_white(composited)
        fill_w = trial.width  / composited.width
        fill_h = trial.height / composited.height
        removed_frac = ((composited.width - trial.width) +
                        (composited.height - trial.height)) / (composited.width + composited.height)
        tight_product_shot = removed_frac <= 0.03 and white_ratio >= 0.95
        if fill_w > 0.85 and fill_h > 0.85 and not tight_product_shot:
            log.info(f"    [fills-frame {fill_w:.0%}×{fill_h:.0%}] → sin padding")
            use_padding = False
        elif fill_w >= 0.98 and not tight_product_shot:
            # Rellena el ancho pero tenía espaciado solo vertical (ej. PrestaShop
            # 1000×1188 con producto a ras de los bordes laterales). No añadir
            # padding lateral; el centrado vertical en el canvas ya da el margen.
            log.info(f"    [fills-width {fill_w:.0%}×{fill_h:.0%}] → sin padding lateral")
            use_padding = False
            composited = trial
            already_cropped = True
        else:
            composited = trial
            already_cropped = True

    log.info(f"    [{'padding 5%' if use_padding else 'sin padding'}]")

    # Autocrop si aún no se hizo: padding normal, o forzar sin padding sobre fondo blanco
    if use_padding and not already_cropped:
        composited = autocrop_white(composited)
    elif force_padding is False and detected_white:
        composited = autocrop_white(composited)

    max_w = int(TARGET_SIZE[0] * (1 - 2 * PADDING)) if use_padding else TARGET_SIZE[0]
    max_h = int(TARGET_SIZE[1] * (1 - 2 * PADDING)) if use_padding else TARGET_SIZE[1]
    ratio = composited.width / composited.height
    new_w = max_w if ratio >= 1 else int(max_h * ratio)
    new_h = int(max_w / ratio) if ratio >= 1 else max_h
    resized = composited.resize((new_w, new_h), Image.LANCZOS)

    canvas = Image.new("RGB", TARGET_SIZE, (255, 255, 255))
    canvas.paste(resized, ((TARGET_SIZE[0] - new_w) // 2,
                           (TARGET_SIZE[1] - new_h) // 2))
    return canvas


def process_image_webp_only(img: Image.Image) -> Image.Image:
    """
    Conversión mínima a WebP: elimina transparencia (composite sobre blanco)
    y convierte a RGB. Mantiene tamaño y resolución originales.
    """
    has_alpha = (img.mode in ("RGBA", "LA") or
                 (img.mode == "P" and "transparency" in img.info))
    if has_alpha:
        return composite_on_white(img)
    return img.convert("RGB")


def to_webp_b64(img: Image.Image) -> str:
    """Convierte imagen PIL a WebP base64 con perfil ICC sRGB embebido."""
    from PIL import ImageCms
    icc_bytes = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    buf = BytesIO()
    img.save(buf, format="WEBP", quality=WEBP_QUALITY, method=6, icc_profile=icc_bytes)
    return base64.b64encode(buf.getvalue()).decode()


def to_webp_srgb_b64(img: Image.Image) -> str:
    """Alias de to_webp_b64 — sRGB ya es el estándar por defecto."""
    return to_webp_b64(img)


def is_high_res(raw: bytes, min_dim: int = MIN_DIM) -> tuple:
    """
    Comprueba si la imagen cumple resolución mínima (min_dim px por lado).
    Devuelve (ok, width, height).
    """
    try:
        img = Image.open(BytesIO(raw))
        w, h = img.size
        return (w >= min_dim and h >= min_dim), w, h
    except Exception:
        return False, 0, 0


def _phash(img: Image.Image, size: int = 8) -> int:
    """
    Perceptual hash sencillo (average-hash). Detecta imágenes visualmente
    equivalentes aunque tengan distinto tamaño, formato o compresión.
    Devuelve un entero de 64 bits (size=8) — distancia Hamming = similitud.
    """
    g = img.convert("L").resize((size, size), Image.Resampling.LANCZOS)
    pixels = list(g.getdata())
    avg = sum(pixels) / len(pixels)
    bits = 0
    for i, p in enumerate(pixels):
        if p >= avg:
            bits |= 1 << i
    return bits


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def dedupe_images(raw_images: list, hamming_threshold: int = 8) -> list:
    """
    Filtra duplicados perceptuales de una lista [(raw_bytes, ext), ...].
    Agrupa imágenes con distancia Hamming ≤ threshold (sobre pHash 8×8) y
    de cada grupo mantiene la de mayor área (ancho × alto). Conserva el
    orden de aparición de la primera imagen de cada grupo.
    Devuelve la lista filtrada [(raw_bytes, ext), ...].
    """
    if len(raw_images) <= 1:
        return raw_images

    entries = []
    for idx, (raw, ext) in enumerate(raw_images):
        try:
            img = Image.open(BytesIO(raw))
            w, h = img.size
            entries.append({
                "idx":     idx,
                "raw":     raw,
                "ext":     ext,
                "area":    w * h,
                "size":    (w, h),
                "nbytes":  len(raw),
                "hash":    _phash(img),
            })
        except Exception as e:
            log.warning(f"  [dedupe] no pude abrir imagen {idx}: {e}")

    # Agrupar por similitud perceptual (transitiva via union-find sencillo)
    groups: list[list[int]] = []
    assigned: dict[int, int] = {}
    for i, e in enumerate(entries):
        target = None
        for g_idx, g in enumerate(groups):
            ref = entries[g[0]]
            if _hamming(e["hash"], ref["hash"]) <= hamming_threshold:
                target = g_idx
                break
        if target is None:
            groups.append([i])
            assigned[i] = len(groups) - 1
        else:
            groups[target].append(i)
            assigned[i] = target

    # De cada grupo, conservar la de mejor calidad.
    # Criterio 1: mayor tamaño de archivo en bytes
    #   → bytes acumulan información; una imagen original tiene más bytes
    #     que una versión cacheada/recomprimida (Magento cache, thumbnails)
    #     incluso cuando la cacheada se ha subescalado a más píxeles.
    # Criterio 2 (desempate): mayor área en píxeles
    # Posición: la del miembro MÁS TEMPRANO del grupo (preserva orden DOM
    # de la web: si la imagen principal aparece primero, se queda primera
    # aunque otra duplicada con más bytes apareciera más tarde).
    kept = []
    for g in groups:
        best = max((entries[i] for i in g),
                   key=lambda e: (e["nbytes"], e["area"]))
        earliest_idx = min(entries[i]["idx"] for i in g)
        kept.append({**best, "idx": earliest_idx})
        if len(g) > 1:
            dropped = [entries[i] for i in g if i != best["idx"]]
            for d in dropped:
                log.info(
                    f"  [dedupe] descartada {d['size']} {d['nbytes']//1024}KB"
                    f" → queda {best['size']} {best['nbytes']//1024}KB"
                    f" (posición {earliest_idx+1})"
                )

    # Ordenar por posición de aparición → respeta orden DOM original
    kept.sort(key=lambda e: e["idx"])
    log.info(f"  [dedupe] {len(raw_images)} → {len(kept)} imágenes únicas")
    return [(e["raw"], e["ext"]) for e in kept]
