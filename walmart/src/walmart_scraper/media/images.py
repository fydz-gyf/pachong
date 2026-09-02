from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

import requests as std_requests
from PIL import Image as PILImage

from ..config import Settings
from ..utils import WALMART_BASE, safe_filename

try:
    from curl_cffi import requests as curl_requests
    HAVE_CURL_CFFI = True
except Exception:
    curl_requests = None
    HAVE_CURL_CFFI = False


class ImageDownloader:
    def __init__(self, settings: Settings):
        self.settings = settings

    def cache_path(self, keyword: str, item_id: str):
        return self.settings.paths.images / safe_filename(keyword) / f"{safe_filename(item_id)}.jpg"

    def _one(self, url: str, path, user_agent: str) -> bool:
        if not url:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1024:
            return True
        headers = {"User-Agent": user_agent, "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8", "Referer": WALMART_BASE + "/"}
        for attempt in range(1, 4):
            try:
                kw = {"headers": headers, "timeout": 30}
                if self.settings.proxy_url:
                    kw["proxies"] = {"http": self.settings.proxy_url, "https": self.settings.proxy_url}
                if HAVE_CURL_CFFI:
                    kw["impersonate"] = self.settings.impersonate
                    r = curl_requests.get(url, **kw)
                else:
                    r = std_requests.get(url, **kw)
                r.raise_for_status()
                img = PILImage.open(BytesIO(r.content)).convert("RGB")
                img.thumbnail((self.settings.image_max_size, self.settings.image_max_size))
                img.save(path, "JPEG", quality=self.settings.image_jpeg_quality, optimize=True)
                return True
            except Exception as e:
                if attempt >= 3:
                    logging.warning("Image download failed: %s -> %s", url, e)
                    return False
                time.sleep(0.8 * attempt + random.random())
        return False

    def download_keyword(self, keyword: str, products: list[dict], user_agent: str) -> dict[str, str]:
        if not self.settings.embed_images:
            return {}
        todo = [(str(p.get("item_id")), p.get("image_url"), self.cache_path(keyword, str(p.get("item_id")))) for p in products if p.get("item_id") and p.get("image_url")]
        result = {}
        lock = threading.Lock()
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, self.settings.image_workers)) as pool:
            futures = {pool.submit(self._one, url, path, user_agent): (item_id, path) for item_id, url, path in todo}
            for fut in as_completed(futures):
                item_id, path = futures[fut]
                if fut.result():
                    result[item_id] = str(path)
                with lock:
                    done += 1
                    if done % 20 == 0 or done == len(todo):
                        logging.info("[%s] image progress %s/%s", keyword, done, len(todo))
        return result
