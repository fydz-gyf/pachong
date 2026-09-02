# -*- coding: utf-8 -*-
"""
Sorftime / Walmart CDP network diagnostic listener
Windows + AdsPower SunBrowser

Purpose:
- Auto-discover the running AdsPower SunBrowser profile
- Read its dynamic DevToolsActivePort
- Attach to Walmart page targets and Sorftime extension targets
- Capture Fetch/XHR requests, payloads, initiators, response headers and text/JSON bodies
- Highlight records containing Walmart product IDs you care about

Usage:
    python sorftime_cdp_sniffer.py
or:
    python sorftime_cdp_sniffer.py --product-id 18208923153 --product-id 20044974964

Keep AdsPower + the Walmart page open while running.
Press Ctrl+C to stop.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import websocket

EXTENSION_ID_DEFAULT = "aadiiicebnjmjmibjengdohedcfeekeg"
MAX_BODY_BYTES_DEFAULT = 2_000_000
CAPTURE_RESOURCE_TYPES = {"Fetch", "XHR", "Document", "Other"}


def now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def safe_filename(text: str, max_len: int = 160) -> str:
    text = re.sub(r'[\\/:*?"<>|]+', "_", text)
    text = re.sub(r"\s+", "_", text).strip("._ ")
    return (text or "item")[:max_len]


def powershell_json(command: str) -> Any:
    full = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        command,
    ]
    p = subprocess.run(
        full,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or f"PowerShell exited {p.returncode}")
    text = p.stdout.strip()
    if not text:
        return None
    return json.loads(text)


@dataclass
class BrowserCandidate:
    pid: int
    name: str
    command_line: str
    user_data_dir: str
    port: int
    browser_ws: str
    browser_name: str


def discover_adspower() -> list[BrowserCandidate]:
    ps = r'''
$items = Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -match '^(SunBrowser|sunbrowser|chrome)\.exe$' -and
        $_.CommandLine -match '--user-data-dir='
    } |
    Select-Object Name, ProcessId, CommandLine
$items | ConvertTo-Json -Depth 4 -Compress
'''
    raw = powershell_json(ps)
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]

    seen_dirs: set[str] = set()
    out: list[BrowserCandidate] = []

    for item in raw:
        cmd = str(item.get("CommandLine") or "")
        if re.search(r"--type=(renderer|gpu-process|utility|crashpad-handler)", cmd, re.I):
            continue

        m = re.search(r'--user-data-dir=(?:"([^"]+)"|([^\s]+))', cmd, re.I)
        if not m:
            continue
        user_data_dir = (m.group(1) or m.group(2) or "").strip()
        if not user_data_dir:
            continue

        norm = os.path.normcase(os.path.abspath(user_data_dir))
        if norm in seen_dirs:
            continue
        seen_dirs.add(norm)

        active = Path(user_data_dir) / "DevToolsActivePort"
        if not active.exists():
            continue

        try:
            lines = active.read_text(encoding="utf-8", errors="replace").splitlines()
            port = int(lines[0].strip())
        except Exception:
            continue

        try:
            info = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2).json()
            browser_ws = str(info.get("webSocketDebuggerUrl") or "")
            browser_name = str(info.get("Browser") or "")
            if not browser_ws:
                continue
        except Exception:
            continue

        out.append(
            BrowserCandidate(
                pid=int(item.get("ProcessId") or 0),
                name=str(item.get("Name") or ""),
                command_line=cmd,
                user_data_dir=user_data_dir,
                port=port,
                browser_ws=browser_ws,
                browser_name=browser_name,
            )
        )

    return out


def get_targets(port: int) -> list[dict[str, Any]]:
    r = requests.get(f"http://127.0.0.1:{port}/json", timeout=4)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def target_score(t: dict[str, Any], extension_id: str) -> int:
    url = str(t.get("url") or "")
    typ = str(t.get("type") or "")
    score = 0
    if "walmart.com" in url:
        score += 100
    if extension_id in url:
        score += 90
    if url.startswith("chrome-extension://"):
        score += 50
    if typ in {"page", "service_worker", "background_page", "worker"}:
        score += 10
    return score


class CDPTargetClient:
    def __init__(
        self,
        target: dict[str, Any],
        output_dir: Path,
        product_ids: list[str],
        extension_id: str,
        max_body_bytes: int,
        show_all: bool = False,
    ):
        self.target = target
        self.output_dir = output_dir
        self.product_ids = product_ids
        self.extension_id = extension_id
        self.max_body_bytes = max_body_bytes
        self.show_all = show_all

        self.ws_url = str(target.get("webSocketDebuggerUrl") or "")
        self.target_id = str(target.get("id") or target.get("targetId") or "")
        self.target_type = str(target.get("type") or "")
        self.target_url = str(target.get("url") or "")
        self.title = str(target.get("title") or "")

        self.ws: websocket.WebSocket | None = None
        self.recv_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.id_lock = threading.Lock()
        self.next_id = 1
        self.pending: dict[int, queue.Queue] = {}
        self.pending_lock = threading.Lock()

        self.requests: dict[str, dict[str, Any]] = {}
        self.requests_lock = threading.Lock()

        self.events_path = output_dir / "network_events.jsonl"
        self.hits_path = output_dir / "likely_sorftime_hits.jsonl"
        self.bodies_dir = output_dir / "response_bodies"
        self.bodies_dir.mkdir(parents=True, exist_ok=True)

    def _new_id(self) -> int:
        with self.id_lock:
            i = self.next_id
            self.next_id += 1
            return i

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 8.0) -> dict[str, Any]:
        if not self.ws:
            raise RuntimeError("CDP websocket is not connected")
        msg_id = self._new_id()
        q: queue.Queue = queue.Queue(maxsize=1)
        with self.pending_lock:
            self.pending[msg_id] = q
        payload = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params
        self.ws.send(json.dumps(payload, ensure_ascii=False))
        try:
            reply = q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"CDP timeout: {method}")
        finally:
            with self.pending_lock:
                self.pending.pop(msg_id, None)
        if "error" in reply:
            raise RuntimeError(f"CDP {method} failed: {reply['error']}")
        return reply.get("result") or {}

    def connect(self) -> None:
        if not self.ws_url:
            raise RuntimeError("Target has no webSocketDebuggerUrl")
        self.ws = websocket.create_connection(
            self.ws_url,
            timeout=8,
            suppress_origin=True,
            enable_multithread=True,
        )
        self.recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self.recv_thread.start()

        self.call("Network.enable", {
            "maxTotalBufferSize": 100_000_000,
            "maxResourceBufferSize": 10_000_000,
            "maxPostDataSize": 10_000_000,
        })
        try:
            self.call("Runtime.enable")
        except Exception:
            pass

    def close(self) -> None:
        self.stop_event.set()
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass

    def _recv_loop(self) -> None:
        assert self.ws is not None
        while not self.stop_event.is_set():
            try:
                raw = self.ws.recv()
                if not raw:
                    break
                msg = json.loads(raw)
            except Exception:
                break

            if "id" in msg:
                msg_id = msg.get("id")
                with self.pending_lock:
                    q = self.pending.get(msg_id)
                if q:
                    try:
                        q.put_nowait(msg)
                    except Exception:
                        pass
                continue

            method = msg.get("method")
            params = msg.get("params") or {}
            try:
                self._handle_event(method, params)
            except Exception as e:
                self._write_jsonl(self.events_path, {
                    "kind": "internal_error",
                    "target_url": self.target_url,
                    "error": repr(e),
                    "event_method": method,
                    "ts": time.time(),
                })

    def _handle_event(self, method: str, p: dict[str, Any]) -> None:
        if method == "Network.requestWillBeSent":
            request_id = str(p.get("requestId") or "")
            req = p.get("request") or {}
            typ = str(p.get("type") or "")
            url = str(req.get("url") or "")
            item = {
                "kind": "request",
                "ts": time.time(),
                "target_id": self.target_id,
                "target_type": self.target_type,
                "target_url": self.target_url,
                "request_id": request_id,
                "resource_type": typ,
                "url": url,
                "method": req.get("method"),
                "headers": req.get("headers") or {},
                "postData": req.get("postData"),
                "hasPostData": req.get("hasPostData"),
                "initiator": p.get("initiator") or {},
                "documentURL": p.get("documentURL"),
            }
            with self.requests_lock:
                self.requests[request_id] = item
            self._write_jsonl(self.events_path, item)

            if self._looks_interesting(item):
                self._print_request(item)

        elif method == "Network.responseReceived":
            request_id = str(p.get("requestId") or "")
            resp = p.get("response") or {}
            with self.requests_lock:
                item = self.requests.setdefault(request_id, {"request_id": request_id})
                item["response"] = {
                    "status": resp.get("status"),
                    "statusText": resp.get("statusText"),
                    "mimeType": resp.get("mimeType"),
                    "url": resp.get("url"),
                    "headers": resp.get("headers") or {},
                    "remoteIPAddress": resp.get("remoteIPAddress"),
                    "protocol": resp.get("protocol"),
                    "fromDiskCache": resp.get("fromDiskCache"),
                    "fromServiceWorker": resp.get("fromServiceWorker"),
                }

        elif method == "Network.loadingFinished":
            request_id = str(p.get("requestId") or "")
            with self.requests_lock:
                item = dict(self.requests.get(request_id) or {})
            if not item:
                return
            typ = str(item.get("resource_type") or "")
            if typ not in CAPTURE_RESOURCE_TYPES and not self._looks_interesting(item):
                return
            threading.Thread(
                target=self._capture_body,
                args=(request_id, item),
                daemon=True,
            ).start()

    def _capture_body(self, request_id: str, item: dict[str, Any]) -> None:
        try:
            result = self.call("Network.getResponseBody", {"requestId": request_id}, timeout=10)
        except Exception:
            return

        body = result.get("body")
        if body is None:
            return
        if result.get("base64Encoded"):
            return

        body_text = str(body)
        if len(body_text.encode("utf-8", errors="ignore")) > self.max_body_bytes:
            body_text = body_text[: self.max_body_bytes]

        hit_ids = [pid for pid in self.product_ids if pid and pid in body_text]
        req_text = json.dumps(item, ensure_ascii=False, default=str)
        initiator_text = json.dumps(item.get("initiator") or {}, ensure_ascii=False, default=str)
        url = str(item.get("url") or "")

        extension_hit = (
            self.extension_id in req_text
            or self.extension_id in initiator_text
            or "sorftime" in req_text.lower()
            or "sorftime" in body_text.lower()
        )

        likely = bool(hit_ids or extension_hit)

        record = {
            "kind": "response_body",
            "ts": time.time(),
            "target_id": self.target_id,
            "target_type": self.target_type,
            "target_url": self.target_url,
            "request_id": request_id,
            "url": url,
            "method": item.get("method"),
            "resource_type": item.get("resource_type"),
            "postData": item.get("postData"),
            "initiator": item.get("initiator"),
            "response": item.get("response"),
            "matched_product_ids": hit_ids,
            "extension_hit": extension_hit,
            "body_preview": body_text[:4000],
        }
        self._write_jsonl(self.events_path, record)

        if likely:
            self._write_jsonl(self.hits_path, record)
            body_name = safe_filename(f"{int(time.time()*1000)}_{request_id}_{urlparse(url).netloc}") + ".txt"
            (self.bodies_dir / body_name).write_text(body_text, encoding="utf-8", errors="replace")
            self._print_hit(record, body_name)

    def _looks_interesting(self, item: dict[str, Any]) -> bool:
        url = str(item.get("url") or "").lower()
        text = json.dumps(item, ensure_ascii=False, default=str).lower()
        if self.extension_id.lower() in text:
            return True
        if "sorftime" in text:
            return True
        if any(x in url for x in [
            "sorftime", "seller", "sales", "revenue", "estimate",
            "product", "item", "asin", "walmart"
        ]):
            return True
        post = str(item.get("postData") or "")
        if any(pid in post for pid in self.product_ids if pid):
            return True
        return False

    def _print_request(self, item: dict[str, Any]) -> None:
        if not self.show_all and item.get("resource_type") not in {"Fetch", "XHR"}:
            return
        url = str(item.get("url") or "")
        host = urlparse(url).netloc
        print(f"[REQ] {item.get('resource_type','?'):5} {item.get('method','?'):4} {host} {url[:180]}")

    def _print_hit(self, rec: dict[str, Any], body_name: str) -> None:
        print("\n" + "=" * 100)
        print("[LIKELY HIT]")
        print("Target :", self.target_url)
        print("URL    :", rec.get("url"))
        print("Method :", rec.get("method"))
        print("IDs    :", ", ".join(rec.get("matched_product_ids") or []) or "-")
        print("ExtHit :", rec.get("extension_hit"))
        print("Body   :", str(self.bodies_dir / body_name))
        print("=" * 100 + "\n")

    @staticmethod
    def _write_jsonl(path: Path, obj: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


def choose_browser(cands: list[BrowserCandidate], extension_id: str) -> BrowserCandidate:
    if not cands:
        raise RuntimeError(
            "No live AdsPower/SunBrowser DevToolsActivePort was found. "
            "Open the AdsPower Walmart profile first."
        )

    scored: list[tuple[int, BrowserCandidate]] = []
    for c in cands:
        try:
            targets = get_targets(c.port)
        except Exception:
            targets = []
        score = sum(target_score(t, extension_id) for t in targets)
        scored.append((score, c))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def choose_targets(targets: list[dict[str, Any]], extension_id: str, all_targets: bool) -> list[dict[str, Any]]:
    selected = []
    for t in targets:
        ws = str(t.get("webSocketDebuggerUrl") or "")
        if not ws:
            continue
        url = str(t.get("url") or "")
        typ = str(t.get("type") or "")
        if all_targets:
            if typ in {"page", "service_worker", "background_page", "worker", "shared_worker"}:
                selected.append(t)
            continue
        if "walmart.com" in url or extension_id in url or url.startswith(f"chrome-extension://{extension_id}"):
            selected.append(t)

    if not selected:
        selected = [
            t for t in targets
            if str(t.get("type") or "") == "page" and "walmart.com" in str(t.get("url") or "")
        ]
    return selected


def main() -> int:
    ap = argparse.ArgumentParser(description="Capture Sorftime/Walmart Fetch/XHR traffic through AdsPower CDP.")
    ap.add_argument("--product-id", action="append", default=[], help="Walmart product ID to search for; repeatable")
    ap.add_argument("--extension-id", default=EXTENSION_ID_DEFAULT)
    ap.add_argument("--output", default="")
    ap.add_argument("--all-targets", action="store_true", help="Attach to all page/worker targets, not only Walmart/Sorftime")
    ap.add_argument("--show-all", action="store_true", help="Print more requests to console")
    ap.add_argument("--max-body-bytes", type=int, default=MAX_BODY_BYTES_DEFAULT)
    args = ap.parse_args()

    product_ids = [str(x).strip() for x in args.product_id if str(x).strip()]
    if not product_ids:
        print("Enter Walmart product IDs to search for.")
        print("Multiple IDs can be separated by commas/spaces. Press Enter to capture without ID filtering.")
        raw = input("Product IDs: ").strip()
        if raw:
            product_ids = [x for x in re.split(r"[\s,;]+", raw) if x]

    out_dir = Path(args.output) if args.output else Path.cwd() / "runtime" / "sorftime_sniffer" / now_stamp()
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 100)
    print("Sorftime / Walmart CDP Network Sniffer")
    print("=" * 100)
    print("Searching for AdsPower SunBrowser...")

    cands = discover_adspower()
    browser = choose_browser(cands, args.extension_id)

    print(f"Browser: {browser.name} pid={browser.pid}")
    print(f"CDP    : 127.0.0.1:{browser.port}")
    print(f"Chrome : {browser.browser_name}")
    print(f"Profile: {browser.user_data_dir}")

    targets = get_targets(browser.port)
    selected = choose_targets(targets, args.extension_id, args.all_targets)

    if not selected:
        print("No Walmart or Sorftime extension targets were found.")
        print("Open walmart.com in this AdsPower profile, then run again.")
        return 2

    print("\nTargets:")
    for t in selected:
        print(f"  [{t.get('type')}] {t.get('url')}")

    meta = {
        "started_at": time.time(),
        "browser": asdict(browser),
        "product_ids": product_ids,
        "extension_id": args.extension_id,
        "targets": selected,
    }
    (out_dir / "session.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    clients: list[CDPTargetClient] = []
    for t in selected:
        client = CDPTargetClient(
            target=t,
            output_dir=out_dir,
            product_ids=product_ids,
            extension_id=args.extension_id,
            max_body_bytes=max(100_000, args.max_body_bytes),
            show_all=args.show_all,
        )
        try:
            client.connect()
            clients.append(client)
        except Exception as e:
            print(f"[WARN] Could not attach target {t.get('url')}: {e}")

    if not clients:
        print("Could not attach any CDP targets.")
        return 3

    print("\nCapture started.")
    print("Now go to the AdsPower Walmart window and:")
    print("  1) Open the search-results page you want to inspect")
    print("  2) Press Ctrl+R / refresh once")
    print("  3) Wait until Sorftime monthly sales/revenue values appear")
    print("  4) Come back here and press Ctrl+C")
    print()
    print(f"Output: {out_dir}")
    print("Likely matches: likely_sorftime_hits.jsonl")
    print("All events     : network_events.jsonl")
    print("Bodies         : response_bodies/")
    print()

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping capture...")
    finally:
        for c in clients:
            c.close()

    print(f"Saved to: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
