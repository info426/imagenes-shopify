"""
Shim para vendor 'Farmina Vet Life' → redirige al scraper compartido farmina.py.
Ambas submarcas (N&D y Vet Life) viven en farmina.com/es y comparten caché.
"""
from marcas.farmina import (  # noqa: F401
    scrape_catalog,
    scrape_product_url,
    find_best_match,
    save_catalog,
    title_cache_key,
    CATALOG_PATH,
    MATCH_THRESHOLD,
)
