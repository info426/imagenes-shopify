"""
Reconocimiento de producto por imagen — "Google Lens" para el matching de URLs.

Compara la(s) foto(s) del producto Shopify contra las imágenes de cada candidato
del catálogo del fabricante y devuelve una similitud visual [0,1]. Sirve como
segundo eje (junto al texto) para confirmar o RECHAZAR un candidato textual:
si el texto coincide pero la foto no, el candidato se descarta (→ sin_match).

Dos backends, elegidos automáticamente:
  - "clip"  → embeddings CLIP locales (open_clip o sentence-transformers).
              Reconoce el MISMO producto aunque la foto sea distinta (ángulo,
              fondo, recorte). Es el comportamiento tipo Google Lens.
  - "hash"  → hash perceptual multi-algoritmo (average + diff + DCT).
              Sin dependencias extra. Detecta cuando fabricante y Shopify usan
              LA MISMA foto (muy común en retail), pero no fotos distintas.

El backend CLIP se importa de forma perezosa. Si torch/open_clip no están
instalados (p. ej. en los workflows de imágenes que no los necesitan), el
módulo degrada solo a "hash" sin romper nada.

Umbrales calibrados por backend en THRESHOLDS[(backend)] = (STRONG, WEAK):
  sim ≥ STRONG → casi seguro el mismo producto
  sim ≤ WEAK   → casi seguro productos distintos
"""

import io
import logging
import os

import requests
from PIL import Image

log = logging.getLogger(__name__)

# Umbrales de similitud por backend (STRONG, WEAK).
# CLIP usa coseno (mismo producto ~0.85+; distinto ~0.55-0.70).
# Hash usa 1 - hamming/64 ponderado (misma foto ~0.92+; distinta foto baja).
THRESHOLDS = {
    "clip": (0.82, 0.62),
    "hash": (0.86, 0.72),
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Referer": "https://www.google.com/",
}

# Desactivar visión por completo con IMAGE_MATCH=0 (fuerza solo-texto en el scraper)
ENABLED = (os.getenv("IMAGE_MATCH", "1") not in ("0", "false", "False"))
# Forzar backend: IMAGE_MATCH_BACKEND=hash|clip (default: auto-detect clip→hash)
_FORCE_BACKEND = os.getenv("IMAGE_MATCH_BACKEND", "").strip().lower()

# Caché en memoria: url → bytes  y  url → embedding/hash (no re-descargar/recodificar)
_BYTES_CACHE: dict = {}
_FEAT_CACHE: dict = {}

# Estado CLIP (lazy)
_CLIP = {"tried": False, "ok": False, "kind": None, "model": None,
         "preprocess": None, "torch": None, "util": None}


# ─── Descarga de imágenes ─────────────────────────────────────────────────────

def default_fetch(url: str, timeout: int = 30) -> bytes | None:
    """Descarga bytes de imagen con cabeceras de navegador. None si falla."""
    if not url:
        return None
    try:
        r = requests.get(url, headers=_HEADERS, timeout=timeout)
        if r.status_code >= 400 or not r.content:
            log.debug(f"  [img] HTTP {r.status_code} {url}")
            return None
        return r.content
    except Exception as e:
        log.debug(f"  [img] fetch error {url}: {e}")
        return None


def _get_bytes(url: str, fetch) -> bytes | None:
    if url in _BYTES_CACHE:
        return _BYTES_CACHE[url]
    data = (fetch or default_fetch)(url)
    _BYTES_CACHE[url] = data
    return data


def _open_rgb(data: bytes) -> Image.Image | None:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img.convert("RGB")
    except Exception as e:
        log.debug(f"  [img] no pude abrir imagen: {e}")
        return None


# ─── Backend CLIP (lazy) ──────────────────────────────────────────────────────

def _init_clip() -> bool:
    """Carga CLIP una sola vez. Devuelve True si está disponible."""
    if _CLIP["tried"]:
        return _CLIP["ok"]
    _CLIP["tried"] = True
    if _FORCE_BACKEND == "hash":
        log.info("[image_match] backend forzado a hash (IMAGE_MATCH_BACKEND=hash)")
        return False
    # 1) open_clip. La fuente de pesos importa MUCHO desde GitHub Actions:
    #    - 'openai'            → CDN de OpenAI (Azure): fiable, sin rate-limit.
    #    - 'laion2b_s34b_b79k' → HuggingFace Hub: da HTTP 429 desde IPs de Actions.
    #    Por eso se prueba 'openai' PRIMERO (Azure) y laion como fallback. El env
    #    IMAGE_CLIP_PRETRAINED permite forzar uno concreto.
    try:
        import time as _time
        import torch  # noqa
        import open_clip
        # (model_name, pretrained). OJO: los pesos 'openai' usan QuickGELU → el
        # nombre de modelo DEBE ser 'ViT-B-32-quickgelu' (si no, embeddings malos).
        forced = os.getenv("IMAGE_CLIP_PRETRAINED", "").strip()
        if forced:
            mn = "ViT-B-32-quickgelu" if forced == "openai" else "ViT-B-32"
            combos = [(mn, forced)]
        else:
            combos = [("ViT-B-32-quickgelu", "openai"),
                      ("ViT-B-32", "laion2b_s34b_b79k")]
        model = preprocess = None
        loaded_tag = None
        for model_name, tag in combos:
            for attempt in range(3):
                try:
                    model, _, preprocess = open_clip.create_model_and_transforms(
                        model_name, pretrained=tag
                    )
                    loaded_tag = f"{model_name}/{tag}"
                    break
                except Exception as de:
                    wait = 3 * (2 ** attempt)
                    log.info(f"[image_match] pesos CLIP '{tag}' intento "
                             f"{attempt+1}/3 falló ({de}); reintento en {wait}s")
                    _time.sleep(wait)
            if model is not None:
                break
        if model is None:
            raise RuntimeError("no se pudieron descargar los pesos CLIP (openai/laion)")
        model.eval()
        _CLIP.update({"ok": True, "kind": "open_clip", "model": model,
                      "preprocess": preprocess, "torch": torch})
        log.info(f"[image_match] backend CLIP (open_clip ViT-B-32, pretrained={loaded_tag}) cargado")
        return True
    except Exception as e:
        log.info(f"[image_match] open_clip no disponible ({e}); pruebo sentence-transformers")
    # 2) sentence-transformers
    try:
        import torch  # noqa
        from sentence_transformers import SentenceTransformer, util
        model = SentenceTransformer("clip-ViT-B-32")
        _CLIP.update({"ok": True, "kind": "sentence_transformers",
                      "model": model, "torch": torch, "util": util})
        log.info("[image_match] backend CLIP (sentence-transformers clip-ViT-B-32) cargado")
        return True
    except Exception as e:
        log.info(f"[image_match] CLIP no disponible ({e}) → degrado a hash perceptual")
        return False


def _clip_embed(data: bytes):
    """Devuelve el vector normalizado de la imagen (o None)."""
    img = _open_rgb(data)
    if img is None:
        return None
    torch = _CLIP["torch"]
    try:
        if _CLIP["kind"] == "open_clip":
            tensor = _CLIP["preprocess"](img).unsqueeze(0)
            with torch.no_grad():
                feat = _CLIP["model"].encode_image(tensor)
                feat = feat / feat.norm(dim=-1, keepdim=True)
            return feat[0]
        else:  # sentence_transformers
            with torch.no_grad():
                feat = _CLIP["model"].encode(
                    img, convert_to_tensor=True, normalize_embeddings=True
                )
            return feat
    except Exception as e:
        log.debug(f"  [img] CLIP embed error: {e}")
        return None


# ─── Backend hash perceptual (sin dependencias) ───────────────────────────────

def _ahash(img: Image.Image, n: int = 8) -> int:
    g = img.convert("L").resize((n, n), Image.Resampling.LANCZOS)
    px = list(g.getdata())
    avg = sum(px) / len(px)
    bits = 0
    for i, p in enumerate(px):
        if p >= avg:
            bits |= 1 << i
    return bits


def _dhash(img: Image.Image, n: int = 8) -> int:
    g = img.convert("L").resize((n + 1, n), Image.Resampling.LANCZOS)
    px = list(g.getdata())
    bits, k = 0, 0
    for row in range(n):
        for col in range(n):
            left = px[row * (n + 1) + col]
            right = px[row * (n + 1) + col + 1]
            if left > right:
                bits |= 1 << k
            k += 1
    return bits


def _dct_hash(img: Image.Image, n: int = 8, hi: int = 32) -> int:
    """pHash por DCT (más robusto a brillo/recorte que average-hash)."""
    g = img.convert("L").resize((hi, hi), Image.Resampling.LANCZOS)
    px = list(g.getdata())
    # DCT-II 2D separable, en Python puro sobre 32×32 (barato, una vez por imagen)
    import math
    N = hi
    # precomputar cos
    cos = [[math.cos((math.pi / N) * (x + 0.5) * u) for x in range(N)]
           for u in range(N)]
    # filas
    rows = []
    for r in range(N):
        base = r * N
        row_in = px[base:base + N]
        rows.append([sum(row_in[x] * cos[u][x] for x in range(N)) for u in range(N)])
    # columnas (solo primeras n×n)
    block = [[0.0] * n for _ in range(n)]
    for u in range(n):
        for v in range(n):
            block[u][v] = sum(rows[r][v] * cos[u][r] for r in range(N))
    vals = [block[u][v] for u in range(n) for v in range(n)]
    med = sorted(vals[1:])[len(vals[1:]) // 2]  # mediana sin el término DC
    bits = 0
    for i, val in enumerate(vals):
        if val > med:
            bits |= 1 << i
    return bits


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _hash_features(data: bytes):
    img = _open_rgb(data)
    if img is None:
        return None
    return (_ahash(img), _dhash(img), _dct_hash(img))


def _hash_sim(fa, fb) -> float:
    if not fa or not fb:
        return 0.0
    a_ah, a_dh, a_dc = fa
    b_ah, b_dh, b_dc = fb
    # Ponderar: DCT (robusto) 0.5, dHash 0.3, aHash 0.2
    ham = (0.2 * _hamming(a_ah, b_ah)
           + 0.3 * _hamming(a_dh, b_dh)
           + 0.5 * _hamming(a_dc, b_dc))
    return max(0.0, 1.0 - ham / 64.0)


# ─── API pública ──────────────────────────────────────────────────────────────

def backend() -> str:
    """Devuelve el backend activo: 'clip' o 'hash'."""
    if not ENABLED:
        return "hash"
    return "clip" if _init_clip() else "hash"


# ─── Features serializables (cacheables en el catálogo JSON) ───────────────────
# Una feature es un dict {"b": backend, "v": [...]}:
#   clip → vector normalizado (lista de floats) → similitud = coseno (= dot)
#   hash → [ahash, dhash, dct]                 → similitud = _hash_sim
# Guardarlas en el catálogo evita re-descargar imágenes del fabricante (que dan
# 403 sin navegador) en cada match: se calculan una vez, con el navegador vivo.

def compute_feature(data: bytes) -> dict | None:
    """Feature serializable de unos bytes de imagen, o None."""
    if not data:
        return None
    bk = backend()
    if bk == "clip":
        v = _clip_embed(data)
        if v is None:
            return None
        try:
            return {"b": "clip", "v": [float(x) for x in v.tolist()]}
        except Exception:
            return None
    feats = _hash_features(data)
    return {"b": "hash", "v": list(feats)} if feats else None


def compute_feature_from_url(url: str, fetch=None) -> dict | None:
    """compute_feature() con caché por URL y descarga (default = requests)."""
    if not url:
        return None
    if url in _FEAT_CACHE:
        return _FEAT_CACHE[url]
    feat = compute_feature(_get_bytes(url, fetch))
    _FEAT_CACHE[url] = feat
    return feat


def _cos_list(a: list, b: list) -> float:
    # vectores CLIP ya normalizados → producto escalar = coseno
    s = sum(x * y for x, y in zip(a, b))
    return max(0.0, min(1.0, s))


def feature_similarity(fa: dict, fb: dict) -> float:
    """Similitud [0,1] entre dos features serializables. 0 si faltan o si los
    backends difieren (no se mezclan vectores CLIP con hashes)."""
    if not fa or not fb or fa.get("b") != fb.get("b"):
        return 0.0
    if fa["b"] == "clip":
        return _cos_list(fa["v"], fb["v"])
    return _hash_sim(tuple(fa["v"]), tuple(fb["v"]))


def best_similarity(query_feats: list, cand_feat: dict) -> float:
    """Mejor similitud de un candidato frente a cualquiera de las fotos query."""
    if not cand_feat or not query_feats:
        return 0.0
    return max((feature_similarity(qf, cand_feat) for qf in query_feats),
               default=0.0)


def score_candidates(query_urls: list, candidate_urls: list, fetch=None) -> list:
    """Similitud por candidato (descargando ambas partes). Para scrapers que no
    precomputan features en el catálogo. Usa caché por URL."""
    q_feats = [f for f in (compute_feature_from_url(u, fetch)
                           for u in (query_urls or [])) if f]
    if not q_feats:
        return [0.0] * len(candidate_urls)
    return [best_similarity(q_feats, compute_feature_from_url(cu, fetch))
            for cu in candidate_urls]


def thresholds() -> tuple:
    """(STRONG, WEAK) del backend activo."""
    return THRESHOLDS[backend()]


def gate_threshold() -> float:
    """Umbral del FILTRO de imagen: si la similitud visual del candidato elegido
    es menor que esto, NO se asigna la URL (se deja el campo vacío). Evita
    'inventar' una URL cuando la foto del producto Shopify no coincide con la del
    candidato. Configurable con IMAGE_GATE (float); si no, default por backend."""
    env = os.getenv("IMAGE_GATE", "").strip()
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    # Calibrado por modelo. El modelo 'openai' (quickgelu) da cosenos MÁS BAJOS
    # que laion: basura/otro-tipo ~0.36-0.40, producto real de la marca ~0.70-0.77
    # (datos de Farmina run #24). Gate 0.60 separa real vs basura sin vetar el
    # correcto (RENAL daba 0.71). Override con env IMAGE_GATE.
    # hash: 0.80 (solo confirma fotos casi idénticas).
    return 0.60 if backend() == "clip" else 0.80
