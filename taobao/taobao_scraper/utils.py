"""与业务无关的小工具。"""

from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime
from pathlib import Path

ILLEGAL_FILENAME_CHARS = r'[\\/:*?"<>|]+'


def safe_filename(text: str, max_len: int = 80) -> str:
    text = re.sub(ILLEGAL_FILENAME_CHARS, "_", str(text).strip())
    text = re.sub(r"\s+", "_", text)
    text = text.strip("._ ") or "keyword"
    return text[:max_len]


def sanitize_sheet_name(name: str, used_names: set) -> str:
    name = re.sub(r"[\\/*?:\[\]]", "_", str(name).strip()) or "Sheet"
    name = name[:31]
    base = name
    i = 2
    while name in used_names:
        suffix = f"_{i}"
        name = base[: 31 - len(suffix)] + suffix
        i += 1
    used_names.add(name)
    return name


def normalize_url(url: str) -> str:
    if not url:
        return ""
    url = str(url).strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        # 视频/图片 HTTP 链接一般均支持 HTTPS；Excel 中也更统一
        return "https://" + url[len("http://") :]
    return url


def clean_title(value) -> str:
    if not value:
        return ""
    value = html.unescape(str(value))
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def to_number(value):
    if value is None or value == "":
        return ""
    text = str(value).replace(",", "").replace("¥", "").replace("￥", "").strip()
    try:
        return float(text)
    except Exception:
        return text


def is_truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def atomic_write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def parse_iso_datetime(value) -> datetime | None:
    """解析 checkpoint 里的 updated_at，解析失败返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None
