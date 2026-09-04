from __future__ import annotations

import time
from urllib.parse import urljoin

from curl_cffi import requests as curl_requests

from reddit_scraper.models import BootstrapData, FetchResult, Loader
from reddit_scraper.safety.guard import RequestGuard

REDDIT_ORIGIN = "https://www.reddit.com"


def _float_header(value) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


class RedditHTTPClient:
    def __init__(self, bootstrap: BootstrapData, config: dict, guard: RequestGuard):
        self.bootstrap = bootstrap
        self.config = config
        self.guard = guard
        self.session = curl_requests.Session(impersonate="chrome")
        self.last_request_started = 0.0
        for c in bootstrap.cookies:
            name = str(c.get("name") or "")
            value = str(c.get("value") or "")
            if not name:
                continue
            try:
                self.session.cookies.set(
                    name, value, domain=str(c.get("domain") or ".reddit.com"), path=str(c.get("path") or "/")
                )
            except Exception:
                self.session.cookies.set(name, value)

    def _throttle(self):
        min_interval = float(self.config.get("min_interval_seconds", 1.5))
        elapsed = time.monotonic() - self.last_request_started
        if self.last_request_started and elapsed < min_interval:
            wait = min_interval - elapsed
            print(f"[THROTTLE] wait {wait:.2f}s")
            time.sleep(wait)

    def _headers(self, referer: str, partial: bool = True) -> dict[str, str]:
        h = {
            "Accept": "text/vnd.reddit.partial+html, text/html;q=0.9" if partial else "text/vnd.reddit.hybrid+html, text/html;q=0.9",
            "Referer": referer,
            "X-Original-Referer": "https://www.reddit.com/",
        }
        if self.bootstrap.client_version:
            h["X-Reddit-Client-Version"] = self.bootstrap.client_version
        return h

    def _request(self, method: str, url: str, *, headers: dict[str, str], data=None) -> FetchResult:
        retries = int(self.config.get("network_retries", 2))
        timeout = float(self.config.get("timeout_seconds", 45))
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            self.guard.before_request()
            self._throttle()
            self.last_request_started = time.monotonic()
            try:
                resp = self.session.request(
                    method, url, headers=headers, data=data, timeout=timeout, allow_redirects=True
                )
                result = FetchResult(
                    status_code=int(resp.status_code), text=resp.text,
                    content_type=str(resp.headers.get("content-type") or ""),
                    final_url=str(resp.url),
                    rate_used=_float_header(resp.headers.get("x-ratelimit-used")),
                    rate_remaining=_float_header(resp.headers.get("x-ratelimit-remaining")),
                    rate_reset=_float_header(resp.headers.get("x-ratelimit-reset")),
                )
                self.guard.after_response(result.status_code, result.text, result.rate_remaining, result.rate_reset)
                return result
            except Exception as exc:
                # Safety/HTTP exceptions should propagate immediately.
                from reddit_scraper.exceptions import HardStopHTTP, RequestBudgetReached, VerificationRequired
                if isinstance(exc, (HardStopHTTP, RequestBudgetReached, VerificationRequired)):
                    raise
                last_exc = exc
                if attempt >= retries:
                    break
                wait = 1.5 * (attempt + 1)
                print(f"[HTTP] network error: {exc}; retry in {wait:.1f}s")
                time.sleep(wait)
        raise RuntimeError(f"HTTP request failed after retries: {last_exc}")

    def fetch_post(self, post_url: str) -> FetchResult:
        return self._request("GET", post_url, headers=self._headers("https://www.reddit.com/", partial=False))

    def fetch_comment_forest(self, post_url: str, subreddit: str, bare_post_id: str, sort: str) -> FetchResult:
        url = (
            f"{REDDIT_ORIGIN}/svc/shreddit/comments/r/{subreddit}/{bare_post_id}"
            f"?render-mode=partial&sort={sort}&inline-refresh=true"
        )
        return self._request("GET", url, headers=self._headers(post_url + f"?sort={sort}"))


    def search_posts(
        self, *, keyword: str, sort: str, time_filter: str, subreddit: str = "",
        limit: int = 100, after: str | None = None, count: int = 0,
    ) -> FetchResult:
        from urllib.parse import urlencode

        if subreddit:
            base = f"{REDDIT_ORIGIN}/r/{subreddit}/search.json"
        else:
            base = f"{REDDIT_ORIGIN}/search.json"
        params = {
            "q": keyword,
            "sort": sort,
            "t": time_filter,
            "type": "link",
            "limit": max(1, min(100, int(limit))),
            "raw_json": 1,
            "count": max(0, int(count)),
        }
        if subreddit:
            params["restrict_sr"] = "on"
        if after:
            params["after"] = after
        url = base + "?" + urlencode(params)
        h = {
            "Accept": "application/json, text/plain;q=0.9, */*;q=0.8",
            "Referer": f"{REDDIT_ORIGIN}/search/?q={keyword}",
            "X-Original-Referer": f"{REDDIT_ORIGIN}/",
        }
        if self.bootstrap.client_version:
            h["X-Reddit-Client-Version"] = self.bootstrap.client_version
        return self._request("GET", url, headers=h)

    def fetch_media(self, url: str, *, referer: str = "", max_bytes: int = 0, retries: int | None = None) -> tuple[int, bytes, str, str]:
        """Compatibility byte-buffer media fetch.

        New downloads use fetch_media_to_file() so large videos are streamed to disk.
        This method remains for tests and compatibility with older callers.
        """
        retries = max(0, int(retries if retries is not None else self.config.get("media_network_retries", 1)))
        timeout = float(self.config.get("timeout_seconds", 45))
        headers = {"Accept": "*/*", "Referer": referer or REDDIT_ORIGIN}
        last_flag = "REQUEST_FAILED"
        for attempt in range(retries + 1):
            self._throttle()
            self.last_request_started = time.monotonic()
            try:
                resp = self.session.request("GET", url, headers=headers, timeout=timeout, allow_redirects=True, stream=True)
                status = int(resp.status_code)
                ctype = str(resp.headers.get("content-type") or "").lower()

                # 403 is not transient for this media URL/session: do not hammer it.
                if status == 403:
                    try:
                        resp.close()
                    finally:
                        return status, b"", ctype, "HTTP_403"

                if status == 429 and attempt < retries:
                    retry_after = resp.headers.get("retry-after") if hasattr(resp, "headers") else None
                    try:
                        wait = min(60.0, max(5.0, float(retry_after)))
                    except Exception:
                        wait = min(60.0, 8.0 * (attempt + 1))
                    resp.close()
                    print(f"[MEDIA] HTTP 429; cooldown {wait:.1f}s")
                    time.sleep(wait)
                    continue

                if status in (500, 502, 503, 504) and attempt < retries:
                    resp.close()
                    wait = 3.0 * (attempt + 1)
                    print(f"[MEDIA] HTTP {status}; retry in {wait:.1f}s")
                    time.sleep(wait)
                    continue

                buf = bytearray()
                try:
                    for chunk in resp.iter_content(65536):
                        if not chunk:
                            continue
                        buf.extend(chunk)
                        if max_bytes and len(buf) > max_bytes:
                            return status, bytes(buf), ctype, "TOO_LARGE"
                finally:
                    resp.close()

                prefix = bytes(buf[:512]).lower()
                if ctype.startswith("text/html") or b"<!doctype html" in prefix or b"<html" in prefix:
                    return status, bytes(buf), ctype, "HTML_WRAPPER"
                return status, bytes(buf), ctype, "OK"
            except Exception as exc:
                last_flag = f"REQUEST_EXCEPTION:{type(exc).__name__}:{exc}"
                if attempt >= retries:
                    break
                wait = 2.0 * (attempt + 1)
                print(f"[MEDIA] {type(exc).__name__}; retry in {wait:.1f}s")
                time.sleep(wait)
        return 0, b"", "", last_flag

    def fetch_media_to_file(
        self,
        url: str,
        target,
        *,
        referer: str = "",
        max_bytes: int = 0,
        retries: int | None = None,
    ) -> tuple[int, str, str, int]:
        """Stream one media response to target instead of buffering it in RAM.

        Returns (status_code, content_type, flag, bytes_written).
        The caller normally passes a .part path and renames it after success.
        """
        from pathlib import Path

        target = Path(target)
        retries = max(0, int(retries if retries is not None else self.config.get("media_network_retries", 1)))
        timeout = float(self.config.get("timeout_seconds", 45))
        headers = {"Accept": "*/*", "Referer": referer or REDDIT_ORIGIN}
        last_flag = "REQUEST_FAILED"

        for attempt in range(retries + 1):
            target.unlink(missing_ok=True)
            self._throttle()
            self.last_request_started = time.monotonic()
            resp = None
            try:
                resp = self.session.request("GET", url, headers=headers, timeout=timeout, allow_redirects=True, stream=True)
                status = int(resp.status_code)
                ctype = str(resp.headers.get("content-type") or "").lower()

                if status == 403:
                    resp.close()
                    return status, ctype, "HTTP_403", 0

                if status == 429 and attempt < retries:
                    retry_after = resp.headers.get("retry-after") if hasattr(resp, "headers") else None
                    try:
                        wait = min(60.0, max(5.0, float(retry_after)))
                    except Exception:
                        wait = min(60.0, 8.0 * (attempt + 1))
                    resp.close()
                    print(f"[MEDIA] HTTP 429; cooldown {wait:.1f}s")
                    time.sleep(wait)
                    continue

                if status in (500, 502, 503, 504) and attempt < retries:
                    resp.close()
                    wait = 3.0 * (attempt + 1)
                    print(f"[MEDIA] HTTP {status}; retry in {wait:.1f}s")
                    time.sleep(wait)
                    continue

                if status != 200:
                    resp.close()
                    return status, ctype, f"HTTP_{status}", 0

                content_length = 0
                try:
                    content_length = int(resp.headers.get("content-length") or 0)
                except Exception:
                    content_length = 0
                if max_bytes and content_length and content_length > max_bytes:
                    resp.close()
                    return status, ctype, "TOO_LARGE", 0

                target.parent.mkdir(parents=True, exist_ok=True)
                size = 0
                prefix = bytearray()
                with target.open("wb") as fh:
                    for chunk in resp.iter_content(65536):
                        if not chunk:
                            continue
                        if len(prefix) < 512:
                            need = 512 - len(prefix)
                            prefix.extend(chunk[:need])
                        size += len(chunk)
                        if max_bytes and size > max_bytes:
                            fh.close()
                            target.unlink(missing_ok=True)
                            resp.close()
                            return status, ctype, "TOO_LARGE", size
                        fh.write(chunk)
                resp.close()

                head = bytes(prefix).lower()
                if ctype.startswith("text/html") or b"<!doctype html" in head or b"<html" in head:
                    target.unlink(missing_ok=True)
                    return status, ctype, "HTML_WRAPPER", size
                return status, ctype, "OK", size
            except Exception as exc:
                target.unlink(missing_ok=True)
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass
                last_flag = f"REQUEST_EXCEPTION:{type(exc).__name__}:{exc}"
                if attempt >= retries:
                    break
                wait = 2.0 * (attempt + 1)
                print(f"[MEDIA] {type(exc).__name__}; retry in {wait:.1f}s")
                time.sleep(wait)

        return 0, "", last_flag, 0

    def fetch_loader(self, post_url: str, loader: Loader, sort: str) -> FetchResult:
        url = urljoin(REDDIT_ORIGIN, loader.src)
        h = self._headers(post_url + f"?sort={sort}")
        h["Content-Type"] = "application/x-www-form-urlencoded"
        return self._request(
            "POST", url, headers=h,
            data={"cursor": loader.cursor, "csrf_token": self.bootstrap.csrf_token},
        )
