from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import urlparse

REDDIT_ORIGIN = "https://www.reddit.com"


def stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def normalize_post_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise ValueError("Post URL is empty")
    if raw.startswith("/r/"):
        raw = REDDIT_ORIGIN + raw
    if not re.match(r"^https?://", raw, re.I):
        raw = REDDIT_ORIGIN + "/" + raw.lstrip("/")
    p = urlparse(raw)
    if "reddit.com" not in (p.hostname or "").lower() or "/comments/" not in p.path:
        raise ValueError(f"Not a Reddit post URL: {raw}")
    return f"{REDDIT_ORIGIN}{p.path.rstrip('/')}/"


def parse_input_urls(raw: str, cwd: Path) -> list[str]:
    raw = raw.strip()
    if not raw:
        default_file = cwd / "input" / "urls.txt"
        if default_file.exists():
            raw = str(default_file)
        else:
            return []
    candidate = raw.strip('"')
    p = Path(candidate)
    treat_as_file = False
    if "reddit.com" not in raw.lower() and not re.search(r"https?://", raw, re.I):
        try:
            treat_as_file = p.exists() and p.is_file()
        except OSError:
            treat_as_file = False
    if treat_as_file:
        pieces = []
        for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                pieces.append(line)
    else:
        pieces = [x for x in re.split(r"[\s,]+", raw) if x]
    out: list[str] = []
    seen: set[str] = set()
    for item in pieces:
        url = normalize_post_url(item)
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def parse_keywords(raw: str, cwd: Path) -> list[str]:
    raw = raw.strip()
    if not raw:
        default_file = cwd / "input" / "keywords.txt"
        if default_file.exists():
            raw = str(default_file)
        else:
            return []
    candidate = raw.strip('"')
    p = Path(candidate)
    treat_as_file = False
    try:
        treat_as_file = p.exists() and p.is_file()
    except OSError:
        treat_as_file = False
    if treat_as_file:
        pieces = [
            line.strip() for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    else:
        # Use | or comma as separators; spaces remain part of a keyword phrase.
        pieces = [x.strip() for x in re.split(r"[|,]+", raw) if x.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for item in pieces:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out
