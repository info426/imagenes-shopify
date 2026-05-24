"""
Gestor de instancia Playwright compartida.

sync_playwright().start() crea un loop asyncio interno. Si dos módulos lo
llaman en el mismo hilo (p. ej. un scraper + core/amazon.py), el segundo
falla con "Playwright Sync API inside the asyncio loop".

Solución: un único sync_playwright por proceso. Todos los scrapers obtienen
sus browsers/contextos desde esta instancia compartida.
"""
import atexit
import logging

log = logging.getLogger(__name__)

_pw = None


def get_playwright():
    """Devuelve la instancia sync_playwright compartida, iniciándola si es necesario."""
    global _pw
    if _pw is None:
        from playwright.sync_api import sync_playwright
        _pw = sync_playwright().start()
        atexit.register(_stop)
        log.debug("[playwright_shared] instancia iniciada")
    return _pw


def _stop():
    global _pw
    if _pw is not None:
        try:
            _pw.stop()
        except Exception:
            pass
        _pw = None
