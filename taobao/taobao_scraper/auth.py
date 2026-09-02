"""登录态管理：本地持久化、CDP 刷新、MTop 签名。

优先使用本地 taobao_auth_state.json；不存在或失效时才回落到 Chrome CDP。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime

import requests

from . import cdp
from .config import APP_KEY, Settings
from .errors import AuthStateError
from .utils import atomic_write_json

CDP_START_HINT = (
    "请先用以下方式启动 Chrome 并登录淘宝：\n"
    '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
    "--remote-debugging-port=9222 --remote-allow-origins=* "
    '--user-data-dir="C:\\ChromeTaobaoCDP"'
)


# ============================================================
# Cookie 序列化
# ============================================================

def cookies_to_serializable(cookie_jar) -> list[dict]:
    out = []
    for c in cookie_jar:
        out.append(
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain or "",
                "path": c.path or "/",
                "expires": c.expires,
                "secure": bool(c.secure),
            }
        )
    return out


def cdp_cookies_to_serializable(browser_cookies: list) -> list[dict]:
    return [
        {
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": c.get("domain", ""),
            "path": c.get("path", "/"),
            "expires": c.get("expires"),
            "secure": bool(c.get("secure", False)),
        }
        for c in browser_cookies
        if c.get("name")
    ]


def add_cookie_to_session(session: requests.Session, c: dict) -> None:
    name = c.get("name")
    if not name:
        return
    value = c.get("value", "")
    domain = c.get("domain") or None
    path = c.get("path") or "/"
    expires = c.get("expires")

    kwargs = {"path": path}
    if domain:
        kwargs["domain"] = domain
    if isinstance(expires, (int, float)) and expires > 0:
        kwargs["expires"] = int(expires)

    try:
        session.cookies.set(name, value, **kwargs)
    except Exception:
        session.cookies.set(name, value)


# ============================================================
# 本地登录态读写
# ============================================================

def save_auth_state(
    session: requests.Session,
    environment: dict,
    settings: Settings,
) -> None:
    payload = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "environment": environment,
        "cookies": cookies_to_serializable(session.cookies),
    }
    atomic_write_json(settings.auth_state_file, payload)


def save_auth_state_from_cdp(settings: Settings) -> dict:
    environment = cdp.read_browser_environment(settings.cdp_host, settings.cdp_port)
    browser_cookies = cdp.read_taobao_cookies_from_cdp(
        settings.cdp_host, settings.cdp_port
    )
    payload = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "environment": environment,
        "cookies": cdp_cookies_to_serializable(browser_cookies),
    }
    atomic_write_json(settings.auth_state_file, payload)
    logging.info(f"已更新登录状态: {settings.auth_state_file}")
    return payload


def load_auth_state(settings: Settings) -> dict | None:
    path = settings.auth_state_file
    if not path.exists():
        return None
    try:
        import json

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not data.get("cookies"):
            return None
        return data
    except Exception as e:
        logging.warning(f"读取 auth state 失败: {e}")
        return None


# ============================================================
# Session 构建
# ============================================================

def make_session_from_state(state: dict) -> tuple[requests.Session, dict]:
    env = state.get("environment") or {}
    environment = {
        "userAgent": env.get("userAgent") or cdp.DEFAULT_USER_AGENT,
        "screenResolution": env.get("screenResolution") or "1920x1080",
        "viewResolution": env.get("viewResolution") or "1920x945",
    }

    session = requests.Session()
    for c in state.get("cookies", []):
        add_cookie_to_session(session, c)

    session.headers.update(
        {
            "User-Agent": environment["userAgent"],
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://s.taobao.com/",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
    )
    return session, environment


def prepare_session(settings: Settings) -> tuple[requests.Session, dict]:
    """
    优先使用本地 auth state；不存在时从 9222 Chrome 获取。
    所以首次需要 Chrome，以后 auth state 未过期时可直接纯 Python 运行。
    """
    state = load_auth_state(settings)
    if state is None:
        if not settings.use_cdp_fallback or not cdp.cdp_available(
            settings.cdp_host, settings.cdp_port
        ):
            raise AuthStateError(
                f"没有可用的 {settings.auth_state_file.name}，"
                f"也连接不到 Chrome {settings.cdp_port}。\n" + CDP_START_HINT
            )
        state = save_auth_state_from_cdp(settings)

    return make_session_from_state(state)


def refresh_session_from_cdp(
    session: requests.Session,
    settings: Settings,
) -> dict:
    if not settings.use_cdp_fallback or not cdp.cdp_available(
        settings.cdp_host, settings.cdp_port
    ):
        raise AuthStateError(
            f"淘宝登录态已失效，且当前没有可用的 Chrome {settings.cdp_port}。\n"
            f"请启动 {settings.cdp_port} Chrome、登录淘宝后重新运行。"
        )

    state = save_auth_state_from_cdp(settings)
    session.cookies.clear()
    for c in state["cookies"]:
        add_cookie_to_session(session, c)
    session.headers["User-Agent"] = state["environment"]["userAgent"]
    return state["environment"]


# ============================================================
# MTop 签名
# ============================================================

def get_cookie_value(session: requests.Session, name: str) -> str:
    matches = [c for c in session.cookies if c.name == name]
    if not matches:
        return ""

    # 优先淘宝主域
    for c in matches:
        if c.domain in {".taobao.com", "taobao.com"}:
            return c.value
    return matches[-1].value


def get_mtop_token(session: requests.Session) -> str:
    value = get_cookie_value(session, "_m_h5_tk")
    if not value:
        return ""
    return value.split("_", 1)[0].strip()


def calculate_sign(token: str, timestamp: int, data_string: str) -> str:
    raw = f"{token}&{timestamp}&{APP_KEY}&{data_string}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()
