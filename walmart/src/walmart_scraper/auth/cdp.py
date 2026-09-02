from __future__ import annotations

import itertools
import json
import logging
import time
from datetime import datetime

import requests
import websocket

from ..config import Settings
from ..utils import atomic_write_json, default_user_agent, extract_title


class CDPClient:
    def __init__(self, websocket_url: str):
        self.ws = websocket.create_connection(
            websocket_url,
            timeout=5,
            http_proxy_host=None,
            suppress_origin=True,
        )
        self.counter = itertools.count(1)

    def call(self, method: str, params=None, timeout: int = 10):
        request_id = next(self.counter)
        payload = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.ws.send(json.dumps(payload))
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            data = json.loads(raw)
            if data.get("id") != request_id:
                continue
            if "error" in data:
                raise RuntimeError(f"CDP {method} failed: {data['error']}")
            return data.get("result", {})
        raise TimeoutError(f"CDP timeout: {method}")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


class CDPAuthProvider:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._http = requests.Session()
        self._http.trust_env = False

    @property
    def base_url(self):
        return f"http://{self.settings.cdp_host}:{self.settings.cdp_port}"

    def available(self) -> bool:
        try:
            self._http.get(self.base_url + "/json/version", timeout=4).raise_for_status()
            return True
        except Exception:
            return False

    def _version(self) -> dict:
        r = self._http.get(self.base_url + "/json/version", timeout=4)
        r.raise_for_status()
        return r.json()

    def _tabs(self) -> list:
        r = self._http.get(self.base_url + "/json", timeout=4)
        r.raise_for_status()
        return r.json()

    def _pick_walmart_tab(self) -> dict:
        tabs = [x for x in self._tabs() if x.get("type") == "page" and x.get("webSocketDebuggerUrl")]
        if not tabs:
            raise RuntimeError("Browser CDP has no debuggable page tab")

        walmart_tabs = [x for x in tabs if "walmart.com" in str(x.get("url", "")).lower()]
        if walmart_tabs:
            return walmart_tabs[0]

        # Reuse the first normal tab rather than trying to automate a new browser
        # profile. The user remains in control of any verification shown there.
        return tabs[0]

    @staticmethod
    def _runtime_value(client: CDPClient, expression: str, timeout: int = 10):
        result = client.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            timeout=timeout,
        )
        return result.get("result", {}).get("value")

    def read_environment(self) -> dict:
        info = self._version()
        browser = info.get("Browser", "Chrome/151.0.0.0")
        version = browser.split("/", 1)[-1]
        ua = default_user_agent().replace("Chrome/151.0.0.0", f"Chrome/{version}")
        try:
            target = next(
                (
                    x
                    for x in self._tabs()
                    if x.get("type") == "page" and "walmart.com" in x.get("url", "")
                ),
                None,
            )
            if target and target.get("webSocketDebuggerUrl"):
                client = CDPClient(target["webSocketDebuggerUrl"])
                try:
                    ua = self._runtime_value(client, "navigator.userAgent") or ua
                finally:
                    client.close()
        except Exception as e:
            logging.warning("Could not read Walmart tab UA from CDP: %s", e)
        return {"userAgent": ua}

    def read_walmart_cookies(self) -> list[dict]:
        ws_url = self._version().get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("Chrome CDP has no browser WebSocket URL")
        client = CDPClient(ws_url)
        try:
            cookies = client.call("Storage.getCookies").get("cookies", [])
        finally:
            client.close()
        cookies = [c for c in cookies if "walmart.com" in str(c.get("domain", "")).lower()]
        if not cookies:
            raise RuntimeError("Browser has no Walmart cookies. Open walmart.com first.")
        return cookies

    def fetch_html_in_browser(self, url: str, timeout: int | None = None) -> dict:
        """Navigate an existing debuggable tab and return its rendered HTML.

        This intentionally does not solve, click through, or automate CAPTCHA / human
        verification. If Walmart presents a verification page, the caller receives that
        page and can ask the user to complete it manually in the visible browser.
        """
        timeout = int(timeout or self.settings.request_timeout)
        target = self._pick_walmart_tab()
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("Selected browser tab has no debugger WebSocket URL")

        client = CDPClient(ws_url)
        started = time.perf_counter()
        try:
            client.call("Page.enable")
            client.call("Runtime.enable")
            client.call("Page.navigate", {"url": url}, timeout=min(15, timeout))

            deadline = time.monotonic() + timeout
            last_ready = ""
            while time.monotonic() < deadline:
                try:
                    last_ready = str(
                        self._runtime_value(client, "document.readyState", timeout=5) or ""
                    )
                    if last_ready in {"interactive", "complete"}:
                        # Give client-side rendering a short chance to populate search data.
                        time.sleep(1.0)
                        break
                except Exception:
                    pass
                time.sleep(0.35)

            html = str(
                self._runtime_value(
                    client,
                    "document.documentElement ? document.documentElement.outerHTML : ''",
                    timeout=min(15, timeout),
                )
                or ""
            )
            current_url = str(
                self._runtime_value(client, "location.href", timeout=5) or url
            )
            title = str(self._runtime_value(client, "document.title", timeout=5) or extract_title(html))
            return {
                "url": current_url,
                "title": title,
                "html": html,
                "elapsed": time.perf_counter() - started,
                "ready_state": last_ready,
                "target_id": target.get("id", ""),
            }
        finally:
            client.close()

    def fetch_sorftime_html(self, url: str, expected_count: int = 0, timeout: int | None = None) -> dict:
        """Load a Walmart search page and wait for Sorftime extension boards.

        The extension runs only in the visible AdsPower/Chromium profile, so standalone
        HTTP responses cannot contain its injected metrics. This method reuses the
        already-open Walmart tab, waits for Sorftime card boards, performs a gentle
        scroll sweep when necessary, and returns the final rendered HTML. It does not
        interact with human-verification controls.
        """
        timeout = int(timeout or getattr(self.settings, "sorftime_wait_timeout", 18))
        timeout = max(5, timeout)
        target = self._pick_walmart_tab()
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("Selected browser tab has no debugger WebSocket URL")

        client = CDPClient(ws_url)
        started = time.perf_counter()
        last_ready = ""
        board_count = 0
        stable_polls = 0
        last_count = -1
        try:
            client.call("Page.enable")
            client.call("Runtime.enable")
            client.call("Page.navigate", {"url": url}, timeout=min(15, timeout))

            ready_deadline = time.monotonic() + min(timeout, 15)
            while time.monotonic() < ready_deadline:
                try:
                    last_ready = str(self._runtime_value(client, "document.readyState", timeout=5) or "")
                    if last_ready in {"interactive", "complete"}:
                        break
                except Exception:
                    pass
                time.sleep(0.30)

            # Sorftime usually injects all boards without scrolling, but Walmart can lazily
            # mount product cards. Poll first, then gradually scroll if coverage is low.
            deadline = time.monotonic() + timeout
            polls = 0
            while time.monotonic() < deadline:
                polls += 1
                try:
                    board_count = int(self._runtime_value(
                        client,
                        "document.querySelectorAll('[data-sorftime-board=\"1\"], [id^=\"sorftime_asinBoard_\"]').length",
                        timeout=5,
                    ) or 0)
                except Exception:
                    board_count = 0

                if board_count == last_count and board_count > 0:
                    stable_polls += 1
                else:
                    stable_polls = 0
                last_count = board_count

                if expected_count > 0 and board_count >= expected_count:
                    # One extra beat lets the text values settle after the board shells appear.
                    time.sleep(0.6)
                    break
                minimum_good = max(1, int(expected_count * 0.85)) if expected_count > 0 else 1
                if board_count >= minimum_good and stable_polls >= 4:
                    break

                # After the first few polls, advance through the page to trigger lazy cards.
                at_bottom = False
                if polls >= 4:
                    try:
                        scroll_state = self._runtime_value(
                            client,
                            "(() => { const h=Math.max(document.body?.scrollHeight||0, document.documentElement?.scrollHeight||0); const vh=window.innerHeight||800; const step=Math.max(700, Math.floor(vh*0.85)); const y=Math.min(h, (window.scrollY||0)+step); window.scrollTo(0,y); return {y:window.scrollY||y,h,vh}; })()",
                            timeout=5,
                        ) or {}
                        if isinstance(scroll_state, dict):
                            y = float(scroll_state.get("y") or 0)
                            h = float(scroll_state.get("h") or 0)
                            vh = float(scroll_state.get("vh") or 0)
                            at_bottom = h > 0 and y + vh >= h - 40
                    except Exception:
                        pass
                if at_bottom and board_count > 0 and stable_polls >= 2:
                    break
                time.sleep(0.45)

            html = str(self._runtime_value(
                client,
                "document.documentElement ? document.documentElement.outerHTML : ''",
                timeout=min(15, timeout),
            ) or "")
            current_url = str(self._runtime_value(client, "location.href", timeout=5) or url)
            title = str(self._runtime_value(client, "document.title", timeout=5) or extract_title(html))
            try:
                self._runtime_value(client, "window.scrollTo(0,0); true", timeout=3)
            except Exception:
                pass
            return {
                "url": current_url,
                "title": title,
                "html": html,
                "elapsed": time.perf_counter() - started,
                "ready_state": last_ready,
                "target_id": target.get("id", ""),
                "sorftime_board_count": board_count,
            }
        finally:
            client.close()

    def read_extension_storage(self, extension_id: str, key: str):
        """Read one chrome.storage.local value from an installed extension.

        MV3 service workers are ephemeral, so a live extension target may not exist.
        When necessary we temporarily open the extension popup through the browser-level
        CDP Target domain, evaluate chrome.storage.local there, then close the target.
        """
        extension_id = str(extension_id or "").strip()
        key = str(key or "").strip()
        if not extension_id or not key:
            raise ValueError("extension_id and key are required")

        prefix = f"chrome-extension://{extension_id}/"

        def try_target(target: dict):
            ws_url = target.get("webSocketDebuggerUrl")
            if not ws_url:
                return None
            client = CDPClient(ws_url)
            try:
                client.call("Runtime.enable")
                expression = (
                    "new Promise((resolve) => {"
                    "try { chrome.storage.local.get([" + json.dumps(key) + "], "
                    "(r) => resolve((r && r[" + json.dumps(key) + "]) || '')); } "
                    "catch (e) { resolve(''); }"
                    "})"
                )
                return self._runtime_value(client, expression, timeout=10)
            finally:
                client.close()

        # Prefer an already-live extension page/service worker/background target.
        for target in self._tabs():
            if str(target.get("url") or "").startswith(prefix):
                try:
                    value = try_target(target)
                    if value not in (None, ""):
                        return value
                except Exception:
                    pass

        browser_ws = self._version().get("webSocketDebuggerUrl")
        if not browser_ws:
            raise RuntimeError("Chrome CDP has no browser WebSocket URL")
        browser = CDPClient(browser_ws)
        target_id = ""
        try:
            # Sorftime MV3 declares popup.html. A temporary popup target gives us an
            # extension origin where chrome.storage.local is available.
            created = browser.call(
                "Target.createTarget",
                {"url": prefix + "popup.html", "background": True},
                timeout=10,
            )
            target_id = str(created.get("targetId") or "")
            deadline = time.monotonic() + 8
            last_error = None
            while time.monotonic() < deadline:
                try:
                    for target in self._tabs():
                        if target_id and str(target.get("id") or target.get("targetId") or "") != target_id:
                            continue
                        if not str(target.get("url") or "").startswith(prefix):
                            continue
                        value = try_target(target)
                        if value not in (None, ""):
                            return value
                except Exception as e:
                    last_error = e
                time.sleep(0.25)
            if last_error:
                raise RuntimeError(f"Extension storage read failed: {last_error}")
            raise RuntimeError(
                f"Extension {extension_id} storage key {key!r} was empty or unavailable"
            )
        finally:
            if target_id:
                try:
                    browser.call("Target.closeTarget", {"targetId": target_id}, timeout=5)
                except Exception:
                    pass
            browser.close()

    def save_state(self) -> dict:
        state = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "environment": self.read_environment(),
            "cookies": [
                {
                    "name": c.get("name", ""),
                    "value": c.get("value", ""),
                    "domain": c.get("domain", ""),
                    "path": c.get("path", "/"),
                    "expires": c.get("expires"),
                    "secure": bool(c.get("secure", False)),
                }
                for c in self.read_walmart_cookies()
                if c.get("name")
            ],
        }
        atomic_write_json(self.settings.paths.auth_state, state)
        logging.info("Saved Walmart auth state: %s", self.settings.paths.auth_state)
        return state

    def load_state(self) -> dict | None:
        path = self.settings.paths.auth_state
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
            return data if isinstance(data, dict) and isinstance(data.get("cookies"), list) else None
        except Exception as e:
            logging.warning("Could not load auth state: %s", e)
            return None
