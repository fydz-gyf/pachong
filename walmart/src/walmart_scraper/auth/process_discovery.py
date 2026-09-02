from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any

import requests


_REMOTE_PORT_RE = re.compile(r"--remote-debugging-port(?:=|\s+)(\d+)", re.I)
_PROXY_SERVER_RE = re.compile(r'--proxy-server(?:=|\s+)(?:"([^"]+)"|([^\s]+))', re.I)
_USER_DATA_DIR_RE = re.compile(r'--user-data-dir(?:=|\s+)(?:"([^"]+)"|([^\s]+))', re.I)
_ADSPOWER_MARKERS = (
    "adspower",
    "sunbrowser",
    "globalbrowser",
    "cwd_global",
)

# AdsPower itself embeds a CefSharp browser for its desktop UI. That endpoint is
# not the anti-detect profile browser and does not support the Chrome browser
# context APIs we need (for example Storage.getCookies). Never bind to it.
_UNUSABLE_PROCESS_NAMES = {
    "cefsharp.browsersubprocess.exe",
}


class BrowserProcessDiscoveryError(RuntimeError):
    pass


@dataclass
class BrowserProcessCandidate:
    pid: int
    name: str
    executable: str
    command_line: str
    host: str
    port: int
    looks_like_adspower: bool = False
    walmart_tabs: int = 0
    page_tabs: int = 0
    browser: str = ""
    proxy_url: str = ""

    @property
    def label(self) -> str:
        kind = "AdsPower" if self.looks_like_adspower else (self.name or "Chromium")
        walmart = f", walmart_tabs={self.walmart_tabs}" if self.walmart_tabs else ""
        pages = f", page_tabs={self.page_tabs}"
        proxy = f", proxy={self.proxy_url}" if self.proxy_url else ""
        return f"{kind} pid={self.pid} cdp={self.host}:{self.port}{walmart}{pages}{proxy}"


def _ps_process_rows() -> list[dict[str, Any]]:
    """Return Windows processes that expose --remote-debugging-port.

    This intentionally uses the local process table, not AdsPower Local API, so
    it also works when the AdsPower plan does not include Local API access.
    """
    if os.name != "nt":
        return []

    ps = r'''
$ErrorActionPreference = "SilentlyContinue"
$rows = Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match '--remote-debugging-port(?:=|\s+)\d+' } |
  Select-Object ProcessId, Name, ExecutablePath, CommandLine
$rows | ConvertTo-Json -Compress -Depth 3
'''.strip()

    try:
        cp = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
        )
    except Exception as e:
        raise BrowserProcessDiscoveryError(f"Could not inspect Windows browser processes: {e}") from e

    raw = (cp.stdout or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception as e:
        raise BrowserProcessDiscoveryError("Windows process discovery returned invalid JSON") from e
    if isinstance(data, dict):
        data = [data]
    return [x for x in (data or []) if isinstance(x, dict)]


def _extract_user_data_dir(command_line: str) -> str:
    m = _USER_DATA_DIR_RE.search(command_line or "")
    if not m:
        return ""
    return str(m.group(1) or m.group(2) or "").strip()


def _resolve_ephemeral_debug_port(command_line: str) -> int:
    """Resolve Chromium --remote-debugging-port=0 via DevToolsActivePort.

    Chromium chooses a free local TCP port when the flag is set to 0 and writes
    the selected port to <user-data-dir>/DevToolsActivePort. AdsPower 9.x/10.x
    commonly launches SunBrowser this way, so process-table discovery must read
    that file instead of treating port 0 as unusable.
    """
    user_data_dir = _extract_user_data_dir(command_line)
    if not user_data_dir:
        return 0

    active_file = os.path.join(user_data_dir, "DevToolsActivePort")
    try:
        with open(active_file, "r", encoding="utf-8", errors="replace") as f:
            first_line = (f.readline() or "").strip()
        port = int(first_line)
    except (OSError, ValueError, TypeError):
        return 0

    if 0 < port <= 65535:
        return port
    return 0


def _is_browser_main_process(command_line: str) -> bool:
    # Chromium child processes include --type=renderer/gpu-process/etc. The main
    # browser process normally has no --type= switch and is the best row to keep.
    return not bool(re.search(r"(?:^|\s)--type(?:=|\s+)", command_line or "", re.I))


def _probe_cdp(host: str, port: int) -> tuple[dict, list]:
    s = requests.Session()
    s.trust_env = False
    try:
        vr = s.get(f"http://{host}:{port}/json/version", timeout=1.8)
        vr.raise_for_status()
        version = vr.json()
        tr = s.get(f"http://{host}:{port}/json", timeout=1.8)
        tr.raise_for_status()
        tabs = tr.json()
        if not isinstance(version, dict):
            version = {}
        if not isinstance(tabs, list):
            tabs = []
        return version, tabs
    finally:
        s.close()


def discover_remote_debug_browsers() -> list[BrowserProcessCandidate]:
    rows = _ps_process_rows()
    by_port: dict[int, BrowserProcessCandidate] = {}

    for row in rows:
        cmd = str(row.get("CommandLine") or "")
        m = _REMOTE_PORT_RE.search(cmd)
        if not m:
            continue
        declared_port = int(m.group(1))
        if declared_port == 0:
            port = _resolve_ephemeral_debug_port(cmd)
            if not port:
                continue
        else:
            port = declared_port
        if port <= 0 or port > 65535:
            continue

        name = str(row.get("Name") or "")
        exe = str(row.get("ExecutablePath") or "")
        if name.strip().lower() in _UNUSABLE_PROCESS_NAMES:
            continue
        haystack = f"{name}\n{exe}\n{cmd}".lower()
        looks_ads = any(marker in haystack for marker in _ADSPOWER_MARKERS)

        # Many Chromium child processes repeat the same command line/port. Keep
        # one representative per debug port, preferring an AdsPower-looking row.
        existing = by_port.get(port)
        proxy_url = ""
        pm = _PROXY_SERVER_RE.search(cmd)
        if pm:
            proxy_url = str(pm.group(1) or pm.group(2) or "").strip()
            # Chromium accepts bare host:port; requests/curl_cffi needs a scheme.
            if proxy_url and "://" not in proxy_url and "," not in proxy_url and ";" not in proxy_url:
                proxy_url = "http://" + proxy_url
            # Complex per-scheme mappings such as http=...;https=... are not safe
            # to flatten automatically; leave them for explicit --proxy instead.
            if ";" in proxy_url or "," in proxy_url:
                proxy_url = ""

        candidate = BrowserProcessCandidate(
            pid=int(row.get("ProcessId") or 0),
            name=name,
            executable=exe,
            command_line=cmd,
            host="127.0.0.1",
            port=port,
            looks_like_adspower=looks_ads,
            proxy_url=proxy_url,
        )
        if existing is None:
            by_port[port] = candidate
        else:
            existing_main = _is_browser_main_process(existing.command_line)
            candidate_main = _is_browser_main_process(candidate.command_line)
            if (looks_ads and not existing.looks_like_adspower) or (candidate_main and not existing_main):
                by_port[port] = candidate

    live: list[BrowserProcessCandidate] = []
    for candidate in by_port.values():
        try:
            version, tabs = _probe_cdp(candidate.host, candidate.port)
        except Exception:
            continue
        candidate.browser = str(version.get("Browser") or "")
        candidate.page_tabs = sum(
            1
            for tab in tabs
            if isinstance(tab, dict)
            and tab.get("type") == "page"
            and tab.get("webSocketDebuggerUrl")
        )
        candidate.walmart_tabs = sum(
            1
            for tab in tabs
            if isinstance(tab, dict)
            and "walmart.com" in str(tab.get("url") or "").lower()
            and tab.get("type") == "page"
            and tab.get("webSocketDebuggerUrl")
        )
        # A real profile browser should expose at least one debuggable page target.
        # This also protects us from attaching to desktop-app/control-plane CEF ports.
        if candidate.page_tabs <= 0:
            continue
        live.append(candidate)

    live.sort(
        key=lambda x: (
            x.walmart_tabs > 0,
            x.looks_like_adspower,
            x.port,
        ),
        reverse=True,
    )
    return live


def resolve_adspower_cdp(preferred_port: int = 0) -> BrowserProcessCandidate:
    candidates = discover_remote_debug_browsers()

    if preferred_port:
        for item in candidates:
            if item.port == preferred_port:
                return item
        raise BrowserProcessDiscoveryError(
            f"No live Chromium CDP was found on 127.0.0.1:{preferred_port}. "
            "Open the AdsPower profile first, then retry."
        )

    if not candidates:
        raise BrowserProcessDiscoveryError(
            "No usable Chromium CDP endpoint was found. For AdsPower/SunBrowser, "
            "--remote-debugging-port=0 is supported through the profile's DevToolsActivePort file. "
            "Open the Walmart AdsPower profile and keep its browser window running."
        )

    ads = [x for x in candidates if x.looks_like_adspower]
    pool = ads or candidates

    walmart = [x for x in pool if x.walmart_tabs > 0]
    if len(walmart) == 1:
        return walmart[0]
    if len(walmart) > 1:
        ports = ", ".join(str(x.port) for x in walmart)
        raise BrowserProcessDiscoveryError(
            "More than one running AdsPower/Chromium profile has Walmart open. "
            f"Use --adspower-cdp-port to choose one. Candidate ports: {ports}"
        )

    ports = ", ".join(str(x.port) for x in pool)
    if len(pool) == 1:
        raise BrowserProcessDiscoveryError(
            "A usable Chromium CDP profile is running, but it has no walmart.com tab. "
            "Open walmart.com in that AdsPower profile and keep the profile browser window open, "
            f"then retry. Candidate port: {ports}"
        )

    raise BrowserProcessDiscoveryError(
        "Multiple usable AdsPower/Chromium CDP endpoints were found and none has Walmart open. "
        "Open Walmart in the intended AdsPower profile or pass --adspower-cdp-port. "
        f"Candidate ports: {ports}"
    )
