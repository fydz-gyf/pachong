from __future__ import annotations

import logging
import random
import time

from ..config import Settings
from ..exceptions import NetworkTransportError, WalmartBlockError
from ..http.client import WalmartHttpClient
from ..storage.checkpoints import CheckpointStore
from ..utils import atomic_write_json, atomic_write_text, safe_filename


SORFTIME_FIELDS = (
    "sorftime_checked",
    "sorftime_status",
    "sorftime_month_sales",
    "sorftime_month_revenue",
    "sorftime_month_sales_raw",
    "sorftime_month_revenue_raw",
)


class SearchService:
    def __init__(self, settings: Settings, client: WalmartHttpClient, checkpoints: CheckpointStore):
        self.settings = settings
        self.client = client
        self.checkpoints = checkpoints

    @staticmethod
    def _has_sorftime_estimate(product: dict) -> bool:
        return product.get("sorftime_month_sales") not in ("", None) or product.get(
            "sorftime_month_revenue"
        ) not in ("", None)

    @staticmethod
    def _merge_sorftime(target: dict, source: dict) -> None:
        for key in SORFTIME_FIELDS:
            if key in source:
                target[key] = source[key]

    def _enrich_page_with_sorftime(
        self, keyword: str, page_no: int, page_products: list[dict], next_data: dict | None = None
    ) -> tuple[int, bool]:
        if not self.settings.sorftime_enabled or not page_products:
            return 0, True

        expected_ids = [str(p.get("item_id") or "") for p in page_products if p.get("item_id")]
        try:
            payload = self.client.fetch_sorftime_page(
                keyword, page_no, expected_ids, next_data=next_data
            )
        except WalmartBlockError as e:
            logging.warning("[%s] page %s Sorftime skipped: %s", keyword, page_no, e)
            return 0, False
        except Exception as e:
            logging.warning(
                "[%s] page %s Sorftime data was not collected: %s. "
                "Make sure Sorftime is logged in inside the AdsPower Walmart profile.",
                keyword,
                page_no,
                e,
            )
            return 0, False

        metrics = payload.get("metrics") or {}
        matched = 0
        for product in page_products:
            item_id = str(product.get("item_id") or "")
            metric = metrics.get(item_id)
            if not metric:
                product["sorftime_status"] = "not_found"
                # Do not mark checked: a future resume should retry transiently missing boards.
                continue
            product.update(metric)
            product["sorftime_status"] = (
                "estimate" if self._has_sorftime_estimate(product) else "unavailable"
            )
            product["sorftime_checked"] = True
            matched += 1
        return matched, True

    def _repair_checkpoint_sorftime(self, keyword: str, cp: dict, products: list[dict]) -> None:
        """Enrich old V7 checkpoints without forcing Walmart product pages to be re-scraped."""
        if not self.settings.sorftime_enabled or not products:
            return
        pages = sorted(
            {
                int(p.get("page") or 0)
                for p in products
                if int(p.get("page") or 0) > 0
                and int(p.get("page") or 0) <= self.settings.max_pages
                and not p.get("sorftime_checked")
            }
        )
        if not pages:
            return

        logging.info(
            "[%s] Sorftime: enriching %s existing checkpoint page(s) (HTTP-direct mode when available)",
            keyword,
            len(pages),
        )
        last_error = ""
        for index, page_no in enumerate(pages, start=1):
            rows = [p for p in products if int(p.get("page") or 0) == page_no]
            matched, ok = self._enrich_page_with_sorftime(keyword, page_no, rows)
            if not ok:
                last_error = f"Sorftime enrichment failed on page {page_no}"
            cp.update(products=products, sorftime_last_error=last_error)
            self.checkpoints.save(cp)
            logging.info(
                "[%s] Sorftime checkpoint page %s/%s: matched=%s products=%s",
                keyword,
                index,
                len(pages),
                matched,
                len(rows),
            )
            if index < len(pages):
                time.sleep(0.6)

    def scrape_keyword(self, keyword: str):
        cp = self.checkpoints.load(keyword)
        products = [
            p
            for p in (cp.get("products") or [])
            if int(p.get("page") or 0) <= self.settings.max_pages
        ]
        for i, p in enumerate(products, start=1):
            p["global_rank"] = i

        # V9 can add Sorftime metrics to an older checkpoint in-place. For checkpoint
        # pages without raw NEXT_DATA it re-fetches only the Walmart search HTML by HTTP,
        # then calls Sorftime directly; browser scrolling is not required.
        self._repair_checkpoint_sorftime(keyword, cp, products)

        next_page = int(cp.get("next_page") or 1)
        if (
            cp.get("completed")
            and next_page > self.settings.max_pages
            and self.settings.resume
            and not self.settings.force_refresh
        ):
            return products, True
        if next_page > self.settings.max_pages:
            cp["completed"] = True
            cp["products"] = products
            self.checkpoints.save(cp)
            return products, True

        seen = {str(p.get("item_id")) for p in products if p.get("item_id")}
        by_id = {str(p.get("item_id")): p for p in products if p.get("item_id")}
        empty_pages = 0
        for page_no in range(next_page, self.settings.max_pages + 1):
            try:
                result = self.client.fetch_search_page(keyword, page_no)
            except (WalmartBlockError, NetworkTransportError) as e:
                cp.update(
                    products=products,
                    next_page=page_no,
                    completed=False,
                    last_error=str(e),
                )
                self.checkpoints.save(cp)
                logging.error("[%s] %s", keyword, e)
                return products, False

            if self.settings.save_html:
                atomic_write_text(
                    self.settings.paths.raw / f"{safe_filename(keyword)}_page_{page_no}.html",
                    result["html"],
                )
            if self.settings.save_next_data and result.get("next_data"):
                atomic_write_json(
                    self.settings.paths.next_data
                    / f"{safe_filename(keyword)}_page_{page_no}.json",
                    result["next_data"],
                )

            sorftime_matched = 0
            if self.settings.sorftime_enabled and result.get("products"):
                sorftime_matched, _ = self._enrich_page_with_sorftime(
                    keyword, page_no, result["products"], next_data=result.get("next_data")
                )

            new_count = duplicate_count = sponsored_count = 0
            for product in result["products"]:
                if self.settings.filter_sponsored and product.get("is_sponsored"):
                    continue
                item_id = str(product.get("item_id") or "")
                if not item_id:
                    continue
                if item_id in seen:
                    duplicate_count += 1
                    existing = by_id.get(item_id)
                    if existing is not None and product.get("sorftime_checked"):
                        # A duplicate card can occasionally be the one Sorftime populated.
                        self._merge_sorftime(existing, product)
                    continue
                seen.add(item_id)
                product["global_rank"] = len(products) + 1
                products.append(product)
                by_id[item_id] = product
                new_count += 1
                sponsored_count += int(bool(product.get("is_sponsored")))

            logging.info(
                "[%s] page=%s cards=%s products=%s new=%s duplicate=%s ads=%s sorftime=%s elapsed=%.2fs",
                keyword,
                page_no,
                result["raw_count"],
                len(result["products"]),
                new_count,
                duplicate_count,
                sponsored_count,
                sorftime_matched,
                result["elapsed"],
            )
            empty_pages = empty_pages + 1 if new_count == 0 else 0
            cp.update(
                products=products,
                next_page=page_no + 1,
                last_error="",
                completed=page_no >= self.settings.max_pages,
            )
            self.checkpoints.save(cp)

            if result.get("no_results") or empty_pages >= self.settings.max_empty_pages:
                cp["completed"] = True
                self.checkpoints.save(cp)
                break
            if page_no < self.settings.max_pages:
                time.sleep(
                    random.uniform(
                        self.settings.request_delay_min, self.settings.request_delay_max
                    )
                )
        return products, bool(cp.get("completed"))
