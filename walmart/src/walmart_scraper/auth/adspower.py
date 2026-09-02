from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import requests

from ..config import Settings


class AdsPowerAPIError(RuntimeError):
    pass


@dataclass
class AdsPowerBrowserInfo:
    profile_id: str
    debug_host: str
    debug_port: int
    selenium: str = ""
    puppeteer: str = ""
    webdriver: str = ""
    started_by_us: bool = False
    proxy_url: str = ""


class AdsPowerClient:
    """Minimal AdsPower Local API client.

    Security rule: the API key is read from an environment variable and is never
    persisted to checkpoints/auth_state or written to logs.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base = settings.adspower_api_base.rstrip("/")
        self.api_key = os.getenv(settings.adspower_api_key_env, "").strip()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request(self, method: str, path: str, *, params=None, json=None) -> dict:
        url = self.base + path
        try:
            r = requests.request(
                method,
                url,
                params=params,
                json=json,
                headers=self._headers(),
                timeout=self.settings.adspower_api_timeout,
            )
        except Exception as e:
            raise AdsPowerAPIError(f"Cannot connect to AdsPower Local API at {self.base}: {e}") from e

        if r.status_code in {401, 403}:
            raise AdsPowerAPIError(
                "AdsPower Local API rejected authorization. Check Local API access and the API key environment variable."
            )
        try:
            obj = r.json()
        except Exception as e:
            raise AdsPowerAPIError(
                f"AdsPower Local API returned non-JSON response: HTTP {r.status_code}"
            ) from e
        if not isinstance(obj, dict):
            raise AdsPowerAPIError("AdsPower Local API returned an invalid response object")
        if int(obj.get("code", -1)) != 0:
            msg = str(obj.get("msg") or obj.get("message") or "unknown error")
            raise AdsPowerAPIError(f"AdsPower Local API error: {msg}")
        return obj

    @staticmethod
    def _host_port(selenium: str = "", debug_port: str | int | None = None) -> tuple[str, int]:
        selenium = str(selenium or "").strip()
        if selenium:
            raw = selenium if "://" in selenium else "http://" + selenium
            parsed = urlparse(raw)
            if parsed.hostname and parsed.port:
                return parsed.hostname, int(parsed.port)
        if debug_port not in (None, ""):
            return "127.0.0.1", int(debug_port)
        raise AdsPowerAPIError("AdsPower did not return a Selenium/debug port")


    @staticmethod
    def _proxy_url_from_config(config: dict) -> str:
        if not isinstance(config, dict):
            return ""
        if str(config.get("proxy_soft") or "").lower() == "no_proxy":
            return ""
        scheme = str(config.get("proxy_type") or "http").lower().strip()
        if scheme not in {"http", "https", "socks5", "socks5h"}:
            return ""
        host = str(config.get("proxy_host") or "").strip()
        port = str(config.get("proxy_port") or "").strip()
        if not host or not port:
            return ""
        user = str(config.get("proxy_user") or "")
        password = str(config.get("proxy_password") or "")
        auth = ""
        if user:
            auth = quote(user, safe="")
            if password:
                auth += ":" + quote(password, safe="")
            auth += "@"
        return f"{scheme}://{auth}{host}:{port}"

    def query_profile_proxy(self, profile_id: str = "", profile_no: str = "") -> str:
        body = {"page": 1, "limit": 1}
        if profile_id:
            body["profile_id"] = [profile_id]
        elif profile_no:
            body["profile_no"] = [profile_no]
        else:
            return ""
        try:
            obj = self._request("POST", "/api/v2/browser-profile/list", json=body)
            rows = ((obj.get("data") or {}).get("list") or [])
            if not rows:
                return ""
            return self._proxy_url_from_config((rows[0] or {}).get("user_proxy_config") or {})
        except AdsPowerAPIError as e:
            logging.warning("Could not read AdsPower profile proxy configuration: %s", e)
            return ""

    def list_active(self) -> list[dict]:
        """Return locally open profiles. Uses the widely-supported v1 endpoint."""
        obj = self._request("GET", "/api/v1/browser/local-active")
        data = obj.get("data") or {}
        rows = data.get("list") or []
        return [x for x in rows if isinstance(x, dict)]

    def _active_v2(self) -> dict | None:
        params = {}
        if self.settings.adspower_profile_id:
            params["profile_id"] = self.settings.adspower_profile_id
        elif self.settings.adspower_profile_no:
            params["profile_no"] = self.settings.adspower_profile_no
        else:
            return None
        try:
            obj = self._request("GET", "/api/v2/browser-profile/active", params=params)
        except AdsPowerAPIError:
            return None
        data = obj.get("data") or {}
        if str(data.get("status", "")).lower() == "active":
            return data
        return None

    def _start_v2(self) -> tuple[str, dict]:
        body = {}
        if self.settings.adspower_profile_id:
            body["profile_id"] = self.settings.adspower_profile_id
        elif self.settings.adspower_profile_no:
            body["profile_no"] = self.settings.adspower_profile_no
        else:
            raise AdsPowerAPIError("AdsPower profile ID/profile No. is required to start a profile")
        obj = self._request("POST", "/api/v2/browser-profile/start", json=body)
        return str(self.settings.adspower_profile_id or self.settings.adspower_profile_no), (obj.get("data") or {})

    def _start_v1(self) -> tuple[str, dict]:
        if not self.settings.adspower_profile_id:
            raise AdsPowerAPIError("V1 fallback requires --adspower-profile-id")
        pid = self.settings.adspower_profile_id
        obj = self._request("GET", "/api/v1/browser/start", params={"user_id": pid})
        return pid, (obj.get("data") or {})

    def attach_or_start(self) -> AdsPowerBrowserInfo:
        # Explicit profile: reuse if active, otherwise start it.
        selector = self.settings.adspower_profile_id or self.settings.adspower_profile_no
        if selector:
            data = self._active_v2()
            started = False
            if data is None:
                if not self.settings.adspower_auto_start:
                    raise AdsPowerAPIError(
                        "Selected AdsPower profile is not active and automatic start is disabled"
                    )
                try:
                    profile_ref, data = self._start_v2()
                except AdsPowerAPIError as v2_error:
                    logging.warning("AdsPower V2 start failed, trying V1 compatibility endpoint: %s", v2_error)
                    profile_ref, data = self._start_v1()
                started = True
            else:
                profile_ref = str(selector)
            ws = data.get("ws") or {}
            host, port = self._host_port(ws.get("selenium", ""), data.get("debug_port"))
            proxy_url = ""
            if self.settings.adspower_sync_proxy:
                proxy_url = self.query_profile_proxy(
                    profile_id=self.settings.adspower_profile_id,
                    profile_no=self.settings.adspower_profile_no,
                )
            return AdsPowerBrowserInfo(
                profile_id=profile_ref,
                debug_host=host,
                debug_port=port,
                selenium=str(ws.get("selenium") or ""),
                puppeteer=str(ws.get("puppeteer") or ""),
                webdriver=str(data.get("webdriver") or ""),
                started_by_us=started,
                proxy_url=proxy_url,
            )

        # No selector: only reuse an already open profile. This avoids accidentally
        # attaching to the wrong fingerprint when several profiles are active.
        active = self.list_active()
        if not active:
            raise AdsPowerAPIError(
                "No AdsPower profile is open. Pass --adspower-profile-id (or --adspower-profile-no), or open exactly one profile first."
            )
        if len(active) > 1:
            ids = [str(x.get("user_id") or x.get("profile_id") or "?") for x in active[:8]]
            raise AdsPowerAPIError(
                "Multiple AdsPower profiles are open; specify the Walmart profile explicitly. Active IDs: "
                + ", ".join(ids)
            )
        row = active[0]
        ws = row.get("ws") or {}
        host, port = self._host_port(ws.get("selenium", ""), row.get("debug_port"))
        pid = str(row.get("user_id") or row.get("profile_id") or "active-profile")
        proxy_url = self.query_profile_proxy(profile_id=pid) if self.settings.adspower_sync_proxy else ""
        return AdsPowerBrowserInfo(
            profile_id=pid,
            debug_host=host,
            debug_port=port,
            selenium=str(ws.get("selenium") or ""),
            puppeteer=str(ws.get("puppeteer") or ""),
            webdriver=str(row.get("webdriver") or ""),
            started_by_us=False,
            proxy_url=proxy_url,
        )
