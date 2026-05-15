"""Cliente Shopify REST Admin API 2024-10."""

import os
import time
import requests

SHOP_DOMAIN   = os.getenv("SHOP_DOMAIN", "7ev1zx-eg.myshopify.com")
CLIENT_ID     = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
API_VERSION   = "2024-10"


def get_token() -> str:
    resp = requests.post(
        f"https://{SHOP_DOMAIN}/admin/oauth/access_token",
        data={"grant_type": "client_credentials",
              "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise ValueError(f"No se pudo obtener token: {resp.text}")
    return token


def _request(method: str, url: str, **kwargs) -> requests.Response:
    """Wrapper con reintentos automáticos ante rate limit (429)."""
    wait = 5
    for attempt in range(6):
        r = requests.request(method, url, **kwargs)
        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", wait))
            time.sleep(retry_after)
            wait = min(wait * 2, 60)
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r


class ShopifyAPI:
    def __init__(self, token: str):
        self.base = f"https://{SHOP_DOMAIN}/admin/api/{API_VERSION}"
        self.h = {"X-Shopify-Access-Token": token,
                  "Content-Type": "application/json"}

    def get_products(self, vendor: str) -> list:
        products, params = [], {"limit": 250, "vendor": vendor}
        url = f"{self.base}/products.json"
        while url:
            r = _request("GET", url, headers=self.h, params=params, timeout=30)
            products.extend(r.json().get("products", []))
            params, url = {}, None
            link = r.headers.get("Link", "")
            if 'rel="next"' in link:
                for part in link.split(","):
                    if 'rel="next"' in part:
                        url = part.strip().split(";")[0].strip("<>")
        return products

    def get_product(self, pid: int) -> dict:
        r = _request("GET", f"{self.base}/products/{pid}.json",
                     headers=self.h, timeout=30)
        return r.json()["product"]

    def get_images(self, pid: int) -> list:
        r = _request("GET", f"{self.base}/products/{pid}/images.json",
                     headers=self.h, timeout=30)
        return r.json().get("images", [])

    def delete_image(self, pid: int, img_id: int):
        _request("DELETE", f"{self.base}/products/{pid}/images/{img_id}.json",
                 headers=self.h, timeout=30)

    def upload_image(self, pid: int, b64: str, filename: str,
                     alt: str = "", position: int = None) -> dict:
        payload = {"attachment": b64, "filename": filename, "alt": alt}
        if position is not None:
            payload["position"] = position
        r = _request("POST", f"{self.base}/products/{pid}/images.json",
                     headers=self.h, json={"image": payload}, timeout=60)
        return r.json()

    def set_variant_image(self, variant_id: int, image_id: int) -> dict:
        r = _request("PUT", f"{self.base}/variants/{variant_id}.json",
                     headers=self.h,
                     json={"variant": {"id": variant_id, "image_id": image_id}},
                     timeout=30)
        return r.json()
