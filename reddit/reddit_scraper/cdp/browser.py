from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import requests as plain_requests
import websocket

from reddit_scraper.models import BrowserInfo

ATTACH_TYPES = {"page", "iframe", "worker", "service_worker", "shared_worker"}


def _ps_json(script: str):
    p = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=25,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or f"PowerShell exited {p.returncode}")
    text = p.stdout.strip()
    return json.loads(text) if text else None


def discover_browsers() -> list[BrowserInfo]:
    script = r'''
$items = Get-CimInstance Win32_Process |
  Where-Object {
    $_.Name -match '^(SunBrowser|sunbrowser|chrome)\.exe$' -and
    $_.CommandLine -match '--user-data-dir=' -and
    $_.CommandLine -notmatch '--type='
  } | Select-Object Name, ProcessId, CommandLine
$items | ConvertTo-Json -Depth 4 -Compress
'''
    raw = _ps_json(script)
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    out: list[BrowserInfo] = []
    seen: set[str] = set()
    for item in raw:
        cmd = str(item.get("CommandLine") or "")
        m = re.search(r'--user-data-dir=(?:"([^"]+)"|([^\s]+))', cmd, re.I)
        if not m:
            continue
        udd = (m.group(1) or m.group(2) or "").strip()
        key = os.path.normcase(os.path.abspath(udd))
        if not udd or key in seen:
            continue
        seen.add(key)
        active = Path(udd) / "DevToolsActivePort"
        if not active.exists():
            continue
        try:
            port = int(active.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip())
            info = plain_requests.get(f"http://127.0.0.1:{port}/json/version", timeout=3).json()
            ws = str(info.get("webSocketDebuggerUrl") or "")
            if not ws:
                continue
        except Exception:
            continue
        out.append(BrowserInfo(
            name=str(item.get("Name") or ""), pid=int(item.get("ProcessId") or 0),
            user_data_dir=udd, port=port, browser=info.get("Browser"), browser_ws=ws,
        ))
    return out


def _json_targets(port: int) -> list[dict[str, Any]]:
    r = plain_requests.get(f"http://127.0.0.1:{port}/json", timeout=5)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def choose_browser(candidates: list[BrowserInfo]) -> BrowserInfo:
    if not candidates:
        raise RuntimeError("No running AdsPower/SunBrowser CDP profile found")
    ranked: list[tuple[int, BrowserInfo]] = []
    for c in candidates:
        score = 0
        try:
            for t in _json_targets(c.port):
                u = str(t.get("url") or "").lower()
                if "reddit.com" in u:
                    score += 200
                if "/comments/" in u:
                    score += 300
        except Exception:
            pass
        ranked.append((score, c))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return ranked[0][1]


class BrowserCDP:
    def __init__(self, browser_ws: str):
        self.browser_ws = browser_ws
        self.ws: websocket.WebSocket | None = None
        self.stop = threading.Event()
        self.next_id = 1
        self.pending: dict[int, queue.Queue] = {}
        self.lock = threading.RLock()
        self.sessions: dict[str, dict[str, Any]] = {}
        self.target_to_session: dict[str, str] = {}
        self.attaching: set[str] = set()
        self.csrf: str | None = None
        self.client_version: str | None = None
        self.csrf_source: str | None = None
        self.csrf_event = threading.Event()

    def connect(self):
        self.ws = websocket.create_connection(
            self.browser_ws, timeout=10, suppress_origin=True, enable_multithread=True
        )
        self.ws.settimeout(1.0)
        threading.Thread(target=self._recv, daemon=True).start()
        self.call("Target.setDiscoverTargets", {"discover": True})
        result = self.call("Target.getTargets", timeout=20)
        for info in result.get("targetInfos") or []:
            self.maybe_attach(info)

    def close(self):
        self.stop.set()
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass

    def call(self, method: str, params: dict[str, Any] | None = None,
             session_id: str | None = None, timeout: float = 12):
        assert self.ws is not None
        with self.lock:
            mid = self.next_id
            self.next_id += 1
            q = queue.Queue(maxsize=1)
            self.pending[mid] = q
        msg: dict[str, Any] = {"id": mid, "method": method}
        if params:
            msg["params"] = params
        if session_id:
            msg["sessionId"] = session_id
        self.ws.send(json.dumps(msg))
        try:
            reply = q.get(timeout=timeout)
        finally:
            with self.lock:
                self.pending.pop(mid, None)
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']}")
        return reply.get("result") or {}

    def maybe_attach(self, info: dict[str, Any]):
        tid = str(info.get("targetId") or "")
        typ = str(info.get("type") or "")
        if not tid or typ not in ATTACH_TYPES:
            return
        with self.lock:
            if tid in self.attaching or tid in self.target_to_session:
                return
            self.attaching.add(tid)

        def work():
            try:
                res = self.call("Target.attachToTarget", {"targetId": tid, "flatten": True}, timeout=10)
                sid = str(res.get("sessionId") or "")
                if not sid:
                    return
                with self.lock:
                    self.sessions[sid] = dict(info)
                    self.target_to_session[tid] = sid
                try:
                    self.call("Network.enable", {"maxPostDataSize": 5_000_000}, session_id=sid, timeout=10)
                except Exception:
                    pass
            except Exception:
                pass
            finally:
                with self.lock:
                    self.attaching.discard(tid)
        threading.Thread(target=work, daemon=True).start()

    def wait_session(self, target_id: str, timeout: float = 15) -> str:
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                sid = self.target_to_session.get(target_id)
            if sid:
                return sid
            time.sleep(0.1)
        raise TimeoutError("Temporary target was not attached to Browser CDP")

    def get_cookies(self, session_id: str) -> list[dict[str, Any]]:
        result = self.call(
            "Network.getCookies", {"urls": ["https://www.reddit.com/", "https://reddit.com/"]},
            session_id=session_id, timeout=12,
        )
        return list(result.get("cookies") or [])

    def _recv(self):
        assert self.ws is not None
        while not self.stop.is_set():
            try:
                raw = self.ws.recv()
                if not raw:
                    break
                msg = json.loads(raw)
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break
            if "id" in msg:
                with self.lock:
                    q = self.pending.get(msg.get("id"))
                if q:
                    try:
                        q.put_nowait(msg)
                    except Exception:
                        pass
                continue
            method = str(msg.get("method") or "")
            p = msg.get("params") or {}
            sid = str(msg.get("sessionId") or "")
            if method == "Target.targetCreated":
                self.maybe_attach(p.get("targetInfo") or {})
            elif method == "Target.attachedToTarget":
                new_sid = str(p.get("sessionId") or "")
                info = p.get("targetInfo") or {}
                tid = str(info.get("targetId") or "")
                if new_sid:
                    with self.lock:
                        self.sessions[new_sid] = dict(info)
                        if tid:
                            self.target_to_session[tid] = new_sid
                    threading.Thread(target=self._enable_network, args=(new_sid,), daemon=True).start()
            elif method == "Network.requestWillBeSent":
                self._on_request(sid, p)

    def _enable_network(self, sid: str):
        try:
            self.call("Network.enable", {"maxPostDataSize": 5_000_000}, session_id=sid, timeout=10)
        except Exception:
            pass

    @staticmethod
    def _extract_csrf(post_data: str) -> str | None:
        if not post_data or "csrf" not in post_data.lower():
            return None
        try:
            obj = json.loads(post_data)
            if isinstance(obj, dict):
                for key in ("csrf_token", "CSRF", "csrf", "csrfToken"):
                    v = obj.get(key)
                    if isinstance(v, str) and v:
                        return v
        except Exception:
            pass
        from urllib.parse import parse_qs
        try:
            qs = parse_qs(post_data, keep_blank_values=True)
            for key in ("csrf_token", "CSRF", "csrf", "csrfToken"):
                vals = qs.get(key) or []
                if vals and vals[0]:
                    return vals[0]
        except Exception:
            pass
        return None

    def _on_request(self, sid: str, p: dict[str, Any]):
        req = p.get("request") or {}
        url = str(req.get("url") or "")
        if "reddit.com" not in url.lower():
            return
        headers = req.get("headers") or {}
        if not self.client_version:
            for k, v in headers.items():
                if str(k).lower() == "x-reddit-client-version" and v:
                    self.client_version = str(v)
                    break
        if not self.csrf:
            token = self._extract_csrf(str(req.get("postData") or ""))
            if token:
                self.csrf = token
                self.csrf_source = url
                self.csrf_event.set()
