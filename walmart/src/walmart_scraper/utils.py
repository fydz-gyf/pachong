from __future__ import annotations

import html as html_lib
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

WALMART_BASE = "https://www.walmart.com"
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def safe_filename(text: str, max_len: int = 100) -> str:
    text = re.sub(r'[\\/:*?"<>|]+', "_", str(text).strip())
    text = re.sub(r"\s+", "_", text)
    return (text.strip("._ ") or "keyword")[:max_len]


def sanitize_sheet_name(name: str, used: set[str]) -> str:
    name = re.sub(r"[\\/*?:\[\]]", "_", str(name).strip())[:31] or "Sheet"
    base = name
    i = 2
    while name in used:
        suffix = f"_{i}"
        name = (base[:31-len(suffix)] + suffix)[:31]
        i += 1
    used.add(name)
    return name


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", errors="replace")
    os.replace(tmp, path)


def first_nonempty(*values):
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return ""


def get_path(obj: Any, *path: str, default=""):
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def to_number(value: Any):
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return value
    s = re.sub(r"[^0-9.\-]", "", str(value).strip().replace(",", ""))
    if not s:
        return ""
    try:
        n = float(s)
        return int(n) if n.is_integer() else n
    except Exception:
        return value


def normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "sponsored"}


def normalize_walmart_url(value: Any) -> str:
    if isinstance(value, dict):
        value = first_nonempty(value.get("url"), value.get("href"), value.get("src"))
    value = str(value or "").strip()
    if value.startswith("//"):
        return "https:" + value
    if value.startswith("/"):
        return urljoin(WALMART_BASE, value)
    return value


def extract_title(html: str) -> str:
    match = TITLE_RE.search(html or "")
    return re.sub(r"\s+", " ", html_lib.unescape(match.group(1))).strip() if match else ""


def default_user_agent() -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    )
