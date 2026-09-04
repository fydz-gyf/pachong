from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "bootstrap": {"timeout_seconds": 60, "page_load_timeout_seconds": 35},
    "http": {"timeout_seconds": 45, "network_retries": 2, "min_interval_seconds": 1.5},
    "safety": {
        "max_http_requests_per_run": 200,
        "hard_stop_status_codes": [403, 429, 503],
        "rate_remaining_pause_threshold": 20,
        "rate_reset_padding_seconds": 3.0,
        "stop_on_verification": True,
    },
    "scrape": {
        "default_max_comments_per_post": 0,
        "default_sort": "top",
        "allowed_sorts": ["top", "new", "confidence"],
    },
    "search": {
        "default_max_posts_per_keyword": 500,
        "default_sort": "relevance",
        "allowed_sorts": ["relevance", "hot", "top", "new", "comments"],
        "default_time": "all",
        "allowed_times": ["hour", "day", "week", "month", "year", "all"],
        "default_subreddit": "",
    },
    "output": {"runtime_dir": "runtime", "checkpoint_dir": "runtime/checkpoints"},
    "media": {
        "enabled": True,
        "download_images": True,
        "download_videos": True,
        "download_comment_media": False,
        "max_items_per_post": 50,
        "max_file_size_mb": 200,
        "ffmpeg_mux": True,
        "ffmpeg_timeout_seconds": 300,
        "dir_name": "media",
    },
}


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return deepcopy(DEFAULT_CONFIG)
    data = json.loads(p.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("settings.json must contain a JSON object")
    return _merge(DEFAULT_CONFIG, data)
