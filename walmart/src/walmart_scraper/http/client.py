from __future__ import annotations

import logging
import random
import time
from urllib.parse import quote

import requests as std_requests

from ..auth.browser import BrowserAuthProvider
from ..config import Settings
from ..exceptions import NetworkTransportError, ParseStructureError, WalmartBlockError
from ..parsers.search import is_block_page, looks_like_no_results, parse_next_data, parse_search_items
from ..parsers.sorftime import parse_sorftime_metrics
from ..sorftime.client import SorftimeError, SorftimeHttpClient
from ..utils import WALMART_BASE, default_user_agent, extract_title, get_path

try:
    from curl_cffi import requests as curl_requests
    HAVE_CURL_CFFI = True
except Exception:
    curl_requests = None
    HAVE_CURL_CFFI = False


class WalmartHttpClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.browser = BrowserAuthProvider(settings)
        self.browser.prepare_if_requested()
        if not self.settings.proxy_url and self.browser.proxy_url:
            self.settings.proxy_url = self.browser.proxy_url
            logging.info("HTTP proxy synchronized from browser profile")

        auth_state = self.browser.load_state()
        if self.browser.resolved_mode in {"chrome", "adspower-api", "adspower-process"}:
            try:
                auth_state = self.browser.save_state()
                logging.info("Loaded fresh Walmart cookies from %s", self.browser.resolved_mode)
            except Exception as e:
                logging.warning(
                    "Could not preload live Walmart cookies; using saved state if available: %s", e
                )

        self.session, self.environment = self._make_session(auth_state)
        self.sorftime = SorftimeHttpClient(
            settings=self.settings,
            browser=self.browser,
            session=self.session,
            environment=self.environment,
        )

    def _make_session(self, auth_state: dict | None):
        ua = get_path(auth_state or {}, "environment", "userAgent", default=default_user_agent())
        if HAVE_CURL_CFFI:
            session = curl_requests.Session(impersonate=self.settings.impersonate)
            client_name = f"curl_cffi/{self.settings.impersonate}"
        else:
            session = std_requests.Session()
            client_name = "requests fallback"
            logging.warning("curl_cffi not installed; Walmart blocks are more likely.")
        session.headers.update(
            {
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
            }
        )
        self._apply_cookies(session, auth_state or {})
        return session, {"userAgent": ua, "client": client_name}

    @staticmethod
    def _apply_cookies(session, state: dict):
        for c in state.get("cookies", []):
            try:
                kwargs = {"path": c.get("path") or "/"}
                if c.get("domain"):
                    kwargs["domain"] = c["domain"]
                session.cookies.set(c.get("name", ""), c.get("value", ""), **kwargs)
            except Exception:
                pass

    def refresh_from_browser(self):
        if not self.settings.use_cdp_fallback or not self.browser.available():
            raise RuntimeError("Cannot connect to configured browser CDP")
        state = self.browser.save_state()
        try:
            self.session.cookies.clear()
        except Exception:
            pass
        self._apply_cookies(self.session, state)
        ua = get_path(state, "environment", "userAgent", default=default_user_agent())
        self.session.headers["User-Agent"] = ua
        self.environment["userAgent"] = ua
        self.environment["client"] = (
            ("curl_cffi" if HAVE_CURL_CFFI else "requests")
            + f"+{self.browser.resolved_mode}-cookies"
        )

    def _kwargs(self):
        kw = {"timeout": self.settings.request_timeout, "allow_redirects": True}
        if self.settings.proxy_url:
            kw["proxies"] = {"http": self.settings.proxy_url, "https": self.settings.proxy_url}
        return kw

    @staticmethod
    def search_url(keyword: str, page_no: int) -> str:
        return (
            f"{WALMART_BASE}/search?q={quote(keyword, safe='')}"
            f"&page={page_no}&affinityOverride=default"
        )

    @staticmethod
    def _parse_result(url: str, status: int, html: str, elapsed: float, source: str) -> dict:
        next_data = parse_next_data(html)
        products, raw_count = parse_search_items(next_data, 0) if next_data else ([], 0)
        return {
            "url": url,
            "status": status,
            "html": html,
            "next_data": next_data,
            "products": products,
            "raw_count": raw_count,
            "elapsed": elapsed,
            "source": source,
        }

    def _fetch_with_visible_browser(self, keyword: str, page_no: int, url: str) -> dict:
        logging.info(
            "[%s] switching page %s to the already-open browser tab; "
            "verification, if shown, must be completed manually",
            keyword,
            page_no,
        )
        page = self.browser.fetch_html_in_browser(url)
        html = page.get("html", "")
        title = page.get("title", "") or extract_title(html)

        if is_block_page(html, title):
            raise WalmartBlockError(
                "Walmart is showing human verification in the visible browser tab. "
                "Complete it manually in that same AdsPower/Chrome window, wait until a normal "
                "Walmart page is visible, then rerun this command. The scraper does not automate "
                "or bypass human verification."
            )

        next_data = parse_next_data(html)
        products, raw_count = parse_search_items(next_data, page_no) if next_data else ([], 0)
        if products:
            logging.info(
                "[%s] page %s loaded through browser: %s products parsed",
                keyword,
                page_no,
                len(products),
            )
            return {
                "url": page.get("url", url),
                "status": 200,
                "html": html,
                "next_data": next_data,
                "products": products,
                "raw_count": raw_count,
                "elapsed": float(page.get("elapsed") or 0),
                "source": "browser-cdp",
            }

        if looks_like_no_results(next_data, html):
            return {
                "url": page.get("url", url),
                "status": 200,
                "html": html,
                "next_data": next_data,
                "products": [],
                "raw_count": raw_count,
                "elapsed": float(page.get("elapsed") or 0),
                "source": "browser-cdp",
                "no_results": True,
            }

        raise ParseStructureError(
            f"No Walmart products parsed from browser page. title={title!r}, "
            f"NEXT_DATA={bool(next_data)}, ready_state={page.get('ready_state')!r}"
        )

    def _fetch_sorftime_page_browser(
        self, keyword: str, page_no: int, expected_item_ids: list[str] | None = None
    ) -> dict:
        if not self.settings.use_cdp_fallback or not self.browser.available():
            raise RuntimeError(
                "Sorftime browser fallback requires the configured AdsPower/Chromium browser to remain open"
            )

        expected = [str(x) for x in (expected_item_ids or []) if str(x)]
        url = self.search_url(keyword, page_no)
        logging.info(
            "[%s] page %s Sorftime browser fallback: loading visible browser page",
            keyword,
            page_no,
        )
        page = self.browser.fetch_sorftime_html_in_browser(url, expected_count=len(set(expected)))
        html = page.get("html", "")
        title = page.get("title", "") or extract_title(html)
        if is_block_page(html, title):
            raise WalmartBlockError(
                "Walmart is showing human verification in the AdsPower browser during "
                "Sorftime browser fallback. Complete it manually in that same window and rerun."
            )

        metrics = parse_sorftime_metrics(html)
        expected_set = set(expected)
        matched = len(expected_set.intersection(metrics)) if expected_set else len(metrics)
        estimates = (
            sum(
                1
                for item_id in expected_set.intersection(metrics)
                if metrics[item_id].get("sorftime_month_sales") not in ("", None)
                or metrics[item_id].get("sorftime_month_revenue") not in ("", None)
            )
            if expected_set
            else sum(
                1
                for row in metrics.values()
                if row.get("sorftime_month_sales") not in ("", None)
                or row.get("sorftime_month_revenue") not in ("", None)
            )
        )
        logging.info(
            "[%s] page %s Sorftime browser: boards=%s matched=%s/%s estimates=%s elapsed=%.2fs",
            keyword,
            page_no,
            int(page.get("sorftime_board_count") or len(metrics)),
            matched,
            len(expected_set),
            estimates,
            float(page.get("elapsed") or 0),
        )
        return {
            "metrics": metrics,
            "matched": matched,
            "boards": int(page.get("sorftime_board_count") or len(metrics)),
            "elapsed": float(page.get("elapsed") or 0),
            "html": html,
            "url": page.get("url", url),
            "source": "browser",
        }

    def fetch_sorftime_page(
        self,
        keyword: str,
        page_no: int,
        expected_item_ids: list[str] | None = None,
        next_data: dict | None = None,
    ) -> dict:
        """Collect Sorftime estimates, preferring direct HTTP in V9.

        HTTP mode reconstructs the same compressed product payload generated by the
        installed Sorftime extension, reads the extension login token once from
        chrome.storage.local, calls Sorftime's endpoint directly, and decrypts the
        response. It does not load or scroll a browser page for every Walmart page.
        """
        if not self.settings.sorftime_enabled:
            return {"metrics": {}, "matched": 0, "boards": 0, "elapsed": 0.0, "source": "disabled"}

        expected = [str(x) for x in (expected_item_ids or []) if str(x)]
        mode = str(getattr(self.settings, "sorftime_mode", "http") or "http").lower()
        if mode == "browser":
            return self._fetch_sorftime_page_browser(keyword, page_no, expected)

        url = self.search_url(keyword, page_no)
        started = time.perf_counter()
        try:
            source_next_data = next_data
            if not source_next_data:
                # Old V7/V8 checkpoints do not contain raw Walmart NEXT_DATA. Re-fetch
                # just this Walmart search page through the existing HTTP pipeline so
                # the Sorftime payload can be reconstructed without browser scrolling.
                walmart_page = self.fetch_search_page(keyword, page_no)
                source_next_data = walmart_page.get("next_data") or {}
                url = walmart_page.get("url") or url
            metrics = self.sorftime.fetch_metrics(keyword, page_no, url, source_next_data)
            expected_set = set(expected)
            matched = len(expected_set.intersection(metrics)) if expected_set else len(metrics)
            elapsed = time.perf_counter() - started
            return {
                "metrics": metrics,
                "matched": matched,
                "boards": len(metrics),
                "elapsed": elapsed,
                "url": url,
                "source": "http",
            }
        except Exception as e:
            if not getattr(self.settings, "sorftime_browser_fallback", True):
                raise
            logging.warning(
                "[%s] page %s Sorftime HTTP failed: %s; falling back to the visible extension page",
                keyword,
                page_no,
                e,
            )
            return self._fetch_sorftime_page_browser(keyword, page_no, expected)

    def fetch_search_page(self, keyword: str, page_no: int) -> dict:
        url = self.search_url(keyword, page_no)
        block_failures = network_failures = cdp_refreshes = 0
        browser_fallback_attempted = False

        for attempt in range(1, self.settings.max_request_retries + 1):
            logging.info(
                "[%s] page %s attempt %s/%s",
                keyword,
                page_no,
                attempt,
                self.settings.max_request_retries,
            )
            start = time.perf_counter()
            try:
                r = self.session.get(url, headers={"Referer": WALMART_BASE + "/"}, **self._kwargs())
                elapsed = time.perf_counter() - start
                html = r.text
                status = int(getattr(r, "status_code", 0) or 0)
                next_data = parse_next_data(html)
                products, raw_count = (
                    parse_search_items(next_data, page_no) if next_data else ([], 0)
                )
                if products:
                    return {
                        "url": url,
                        "status": status,
                        "html": html,
                        "next_data": next_data,
                        "products": products,
                        "raw_count": raw_count,
                        "elapsed": elapsed,
                        "source": "http",
                    }

                title = extract_title(html)
                if is_block_page(html, title):
                    block_failures += 1
                    logging.warning(
                        "[%s] Walmart verification on page %s: HTTP %s title=%r",
                        keyword,
                        page_no,
                        status,
                        title,
                    )

                    # A browser challenge means refreshing cookies into a separate HTTP
                    # stack is usually not useful. Prefer the already-open visible browser
                    # for this page, but never automate the human-verification step.
                    if (
                        self.settings.browser_page_fallback
                        and not browser_fallback_attempted
                        and self.settings.use_cdp_fallback
                        and self.browser.available()
                    ):
                        browser_fallback_attempted = True
                        return self._fetch_with_visible_browser(keyword, page_no, url)

                    if self.settings.use_cdp_fallback and self.browser.available() and cdp_refreshes < 1:
                        cdp_refreshes += 1
                        try:
                            self.refresh_from_browser()
                            time.sleep(random.uniform(2, 4))
                            continue
                        except Exception as e:
                            logging.warning("Browser cookie refresh failed: %s", e)

                    lo, hi = self.settings.block_backoff_ranges[
                        min(block_failures - 1, len(self.settings.block_backoff_ranges) - 1)
                    ]
                    if attempt < self.settings.max_request_retries:
                        time.sleep(random.uniform(lo, hi))
                        continue
                    raise WalmartBlockError(
                        "Walmart keeps returning human verification. Complete the verification "
                        "manually in the configured browser and rerun."
                    )

                if status >= 500 or status in {408, 425, 429}:
                    raise RuntimeError(f"HTTP {status}")
                if looks_like_no_results(next_data, html):
                    return {
                        "url": url,
                        "status": status,
                        "html": html,
                        "next_data": next_data,
                        "products": [],
                        "raw_count": raw_count,
                        "elapsed": elapsed,
                        "source": "http",
                        "no_results": True,
                    }
                raise ParseStructureError(
                    f"No Walmart products parsed. HTTP={status}, title={title!r}, "
                    f"NEXT_DATA={bool(next_data)}"
                )
            except WalmartBlockError:
                raise
            except ParseStructureError:
                raise
            except Exception as e:
                network_failures += 1
                if network_failures >= self.settings.max_request_retries:
                    raise NetworkTransportError(
                        f"[{keyword}] page {page_no} failed repeatedly: {e}"
                    ) from e
                lo, hi = self.settings.network_backoff_ranges[
                    min(network_failures - 1, len(self.settings.network_backoff_ranges) - 1)
                ]
                time.sleep(random.uniform(lo, hi))
        raise NetworkTransportError(f"[{keyword}] page {page_no} request failed")
