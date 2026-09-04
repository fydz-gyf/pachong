from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def _iso_from_utc(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def parse_search_listing(
    text: str,
    *,
    keyword: str,
    sort: str,
    time_filter: str,
    subreddit_filter: str = "",
    start_rank: int = 1,
) -> tuple[list[dict[str, Any]], str | None]:
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("Reddit search response is not a JSON object")
    data = obj.get("data")
    if not isinstance(data, dict):
        raise ValueError("Reddit search response missing data object")

    rows: list[dict[str, Any]] = []
    rank = start_rank
    children = data.get("children") or []
    if not isinstance(children, list):
        children = []

    for child in children:
        if not isinstance(child, dict):
            continue
        d = child.get("data")
        if not isinstance(d, dict):
            continue
        bare_id = str(d.get("id") or "")
        fullname = str(d.get("name") or (f"t3_{bare_id}" if bare_id else ""))
        permalink = str(d.get("permalink") or "")
        outbound = str(d.get("url_overridden_by_dest") or d.get("url") or "")
        selftext = str(d.get("selftext") or "")
        rows.append({
            "search_keyword": keyword,
            "search_subreddit": subreddit_filter,
            "search_sort": sort,
            "search_time": time_filter,
            "result_rank": rank,
            "post_id": fullname,
            "bare_post_id": bare_id,
            "subreddit": str(d.get("subreddit") or ""),
            "subreddit_id": str(d.get("subreddit_id") or ""),
            "title": str(d.get("title") or ""),
            "author": str(d.get("author") or ""),
            "author_fullname": str(d.get("author_fullname") or ""),
            "created_utc": _iso_from_utc(d.get("created_utc")),
            "score": _int(d.get("score")),
            "upvote_ratio": _float(d.get("upvote_ratio")),
            "comment_count": _int(d.get("num_comments")),
            "permalink": permalink,
            "full_url": ("https://www.reddit.com" + permalink) if permalink.startswith("/") else permalink,
            "outbound_url": outbound,
            "domain": str(d.get("domain") or ""),
            "selftext": selftext,
            "flair": str(d.get("link_flair_text") or ""),
            "nsfw": bool(d.get("over_18")),
            "spoiler": bool(d.get("spoiler")),
            "locked": bool(d.get("locked")),
            "stickied": bool(d.get("stickied")),
            "is_self": bool(d.get("is_self")),
            "post_hint": str(d.get("post_hint") or ""),
            "thumbnail": str(d.get("thumbnail") or ""),
            "distinguished": str(d.get("distinguished") or ""),
        })
        rank += 1

    after = data.get("after")
    return rows, (str(after) if after else None)
