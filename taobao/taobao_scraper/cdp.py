"""Chrome DevTools Protocol 客户端。

仅用于首次获取登录态、以及登录态失效时刷新 Cookie。
正常抓取走纯 HTTP，不依赖 Chrome。
"""

from __future__ import annotations

import itertools
import json
import logging
import time
from typing import Any

import requests
import websocket

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


class CDPClient:
    def __init__(self, websocket_url: str, recv_timeout: int = 5):
        self.ws = websocket.create_connection(
            websocket_url,
            timeout=recv_timeout,
            http_proxy_host=None,
        )
        self.counter = itertools.count(1)

    def call(self, method: str, params: dict | None = None, timeout: int = 10) -> dict:
        request_id = next(self.counter)
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.ws.send(json.dumps(message))

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
                raise RuntimeError(f"CDP {method} 失败: {data['error']}")
            return data.get("result", {})

        raise TimeoutError(f"CDP 调用超时: {method}")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass

    def __enter__(self) -> "CDPClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def cdp_base_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def get_cdp_version_info(host: str, port: int) -> dict:
    r = requests.get(f"{cdp_base_url(host, port)}/json/version", timeout=4)
    r.raise_for_status()
    return r.json()


def get_cdp_tabs(host: str, port: int) -> list:
    r = requests.get(f"{cdp_base_url(host, port)}/json", timeout=4)
    r.raise_for_status()
    return r.json()


def cdp_available(host: str, port: int) -> bool:
    try:
        get_cdp_version_info(host, port)
        return True
    except Exception:
        return False


def _pick_taobao_tab(tabs: list) -> dict | None:
    for tab in tabs:
        if tab.get("type") == "page" and "s.taobao.com" in tab.get("url", ""):
            return tab
    for tab in tabs:
        if tab.get("type") == "page" and "taobao.com" in tab.get("url", ""):
            return tab
    return None


def read_browser_environment(host: str, port: int) -> dict:
    """从淘宝标签页读取 UA / 屏幕信息。找不到页面时使用 Chrome version UA。"""
    info = get_cdp_version_info(host, port)
    chrome_browser = info.get("Browser", "Chrome/151.0.0.0")
    chrome_version = chrome_browser.split("/", 1)[-1]

    result = {
        "userAgent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_version} Safari/537.36"
        ),
        "screenResolution": "1920x1080",
        "viewResolution": "1920x945",
    }

    try:
        target = _pick_taobao_tab(get_cdp_tabs(host, port))
        if not target or not target.get("webSocketDebuggerUrl"):
            return result

        with CDPClient(target["webSocketDebuggerUrl"]) as client:
            js_result = client.call(
                "Runtime.evaluate",
                {
                    "expression": """
                    (() => ({
                        userAgent: navigator.userAgent,
                        screenResolution: screen.width + 'x' + screen.height,
                        viewResolution: window.innerWidth + 'x' + window.innerHeight
                    }))()
                    """,
                    "returnByValue": True,
                },
            )
        value = js_result.get("result", {}).get("value", {})
        if isinstance(value, dict):
            result.update({k: v for k, v in value.items() if v})
    except Exception as e:
        logging.warning(f"读取浏览器环境失败，使用默认值: {e}")

    return result


def read_taobao_cookies_from_cdp(host: str, port: int) -> list:
    info = get_cdp_version_info(host, port)
    ws_url = info.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("Chrome CDP 没有 webSocketDebuggerUrl")

    with CDPClient(ws_url) as client:
        result = client.call("Storage.getCookies")
    all_cookies = result.get("cookies", [])

    cookies = []
    for c in all_cookies:
        domain = str(c.get("domain", "")).lower()
        if "taobao.com" in domain or "tmall.com" in domain:
            cookies.append(c)

    if not cookies:
        raise RuntimeError(f"{port} Chrome 中没有读取到淘宝 Cookie，请先登录淘宝。")

    logging.info(f"CDP读取 Cookie: 全部={len(all_cookies)}, 淘宝相关={len(cookies)}")
    return cookies
