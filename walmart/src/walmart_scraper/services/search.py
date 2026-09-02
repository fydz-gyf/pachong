from __future__ import annotations

import logging
import random
import time

from ..config import Settings
from ..exceptions import NetworkTransportError, WalmartBlockError
from ..http.client import WalmartHttpClient
from ..storage.checkpoints import CheckpointStore
from ..utils import atomic_write_json, atomic_write_text, safe_filename


class SearchService:
    def __init__(self, settings: Settings, client: WalmartHttpClient, checkpoints: CheckpointStore):
        self.settings = settings
        self.client = client
        self.checkpoints = checkpoints

    def scrape_keyword(self, keyword: str):
        cp = self.checkpoints.load(keyword)
        products = [p for p in (cp.get("products") or []) if int(p.get("page") or 0) <= self.settings.max_pages]
        for i, p in enumerate(products, start=1):
            p["global_rank"] = i
        next_page = int(cp.get("next_page") or 1)
        if cp.get("completed") and next_page > self.settings.max_pages and self.settings.resume and not self.settings.force_refresh:
            return products, True
        if next_page > self.settings.max_pages:
            cp["completed"] = True
            self.checkpoints.save(cp)
            return products, True

        seen = {str(p.get("item_id")) for p in products if p.get("item_id")}
        empty_pages = 0
        for page_no in range(next_page, self.settings.max_pages + 1):
            try:
                result = self.client.fetch_search_page(keyword, page_no)
            except (WalmartBlockError, NetworkTransportError) as e:
                cp.update(products=products, next_page=page_no, completed=False, last_error=str(e))
                self.checkpoints.save(cp)
                logging.error("[%s] %s", keyword, e)
                return products, False

            if self.settings.save_html:
                atomic_write_text(self.settings.paths.raw / f"{safe_filename(keyword)}_page_{page_no}.html", result["html"])
            if self.settings.save_next_data and result.get("next_data"):
                atomic_write_json(self.settings.paths.next_data / f"{safe_filename(keyword)}_page_{page_no}.json", result["next_data"])

            new_count = duplicate_count = sponsored_count = 0
            for product in result["products"]:
                if self.settings.filter_sponsored and product.get("is_sponsored"):
                    continue
                item_id = str(product.get("item_id") or "")
                if not item_id:
                    continue
                if item_id in seen:
                    duplicate_count += 1
                    continue
                seen.add(item_id)
                product["global_rank"] = len(products) + 1
                products.append(product)
                new_count += 1
                sponsored_count += int(bool(product.get("is_sponsored")))

            logging.info("[%s] page=%s cards=%s products=%s new=%s duplicate=%s ads=%s elapsed=%.2fs", keyword, page_no, result["raw_count"], len(result["products"]), new_count, duplicate_count, sponsored_count, result["elapsed"])
            empty_pages = empty_pages + 1 if new_count == 0 else 0
            cp.update(products=products, next_page=page_no + 1, last_error="", completed=page_no >= self.settings.max_pages)
            self.checkpoints.save(cp)

            if result.get("no_results") or empty_pages >= self.settings.max_empty_pages:
                cp["completed"] = True
                self.checkpoints.save(cp)
                break
            if page_no < self.settings.max_pages:
                time.sleep(random.uniform(self.settings.request_delay_min, self.settings.request_delay_max))
        return products, bool(cp.get("completed"))
