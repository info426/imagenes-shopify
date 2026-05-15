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


def is_white_background(img_rgb: Image.Image) -> bool:
    """
    Comprueba las 4 esquinas (parche 5%) para determinar si el fondo es blanco.
    Devuelve True si ≥60% de los píxeles muestreados son blancos (≥245,245,245).
    """
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
    return ratio >= WHITE_MIN_FRAC


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

    if force_padding is None:
        use_padding = is_white_background(composited)
    else:
        use_padding = force_padding
        log.info(f"    [bg] forzado: {'padding 5%' if use_padding else 'sin padding'}")

    log.info(f"    [{'padding 5%' if use_padding else 'sin padding'}]")

    # Autocrop siempre que haya padding (para añadirlo limpio) o cuando se
    # fuerza sin padding (para quitar bordes blancos existentes antes de rellenar)
    if use_padding or force_padding is False:
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
    """Convierte imagen PIL a WebP base64 listo para Shopify API."""
    buf = BytesIO()
    img.save(buf, format="WEBP", quality=WEBP_QUALITY, method=6)
    return base64.b64encode(buf.getvalue()).decode()


def is_high_res(raw: bytes) -> tuple:
    """
    Comprueba si la imagen cumple resolución mínima.
    Devuelve (ok, width, height).
    """
    try:
        img = Image.open(BytesIO(raw))
        w, h = img.size
        return (w >= MIN_DIM and h >= MIN_DIM), w, h
    except Exception:
        return False, 0, 0
