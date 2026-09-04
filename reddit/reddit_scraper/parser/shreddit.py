from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup

from reddit_scraper.models import Loader, ParsedPage
from reddit_scraper.parser.media import extract_comment_media


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _comment_body(tag) -> str:
    tid = str(tag.get("thingid") or tag.get("thingId") or "")
    if tid:
        body = tag.find(id=f"{tid}-comment-rtjson-content")
        if body:
            return body.get_text("\n", strip=True)
    body = tag.find(attrs={"slot": "comment"})
    return body.get_text("\n", strip=True) if body else ""


def parse_comments(html_text: str) -> ParsedPage:
    soup = BeautifulSoup(html_text, "html.parser")
    rows: list[dict[str, Any]] = []
    for tag in soup.find_all("shreddit-comment"):
        thing_id = str(tag.get("thingid") or tag.get("thingId") or "")
        body = _comment_body(tag)
        author = str(tag.get("author") or "")
        media = extract_comment_media(tag)
        rows.append({
            "media_urls": " | ".join(m.url for m in media),
            "comment_id": thing_id,
            "post_id": str(tag.get("postid") or tag.get("postId") or ""),
            "parent_id": str(tag.get("parentid") or tag.get("parentId") or ""),
            "depth": _to_int(tag.get("depth")),
            "position": _to_int(tag.get("comment-position")),
            "parent_positions": str(tag.get("comment-parent-positions") or ""),
            "author": author,
            "created": str(tag.get("created") or ""),
            "score": _to_int(tag.get("score")),
            "award_count": _to_int(tag.get("award-count")),
            "permalink": str(tag.get("permalink") or ""),
            "content_type": str(tag.get("content-type") or ""),
            "is_op": tag.has_attr("is-op"),
            "collapsed": tag.has_attr("collapsed"),
            "is_deleted": author.lower() == "[deleted]" or body.strip().lower() in {"[deleted]", "[removed]"},
            "body": body,
        })

    loaders: list[Loader] = []
    for fp in soup.find_all("faceplate-partial"):
        classes = fp.get("class") or []
        src = str(fp.get("src") or "")
        is_more = (
            "more-comments-partial" in classes
            or str(fp.get("id") or "") == "top-level-more-comments-partial"
            or "/svc/shreddit/more-comments/" in src
        )
        if not is_more:
            continue
        inp = fp.find("input", attrs={"name": "cursor"})
        cursor = str(inp.get("value") or "") if inp else ""
        if not src or not cursor:
            continue
        top = "top-level=1" in src
        if not top:
            link = fp.find("a", class_="more-comments-link")
            top = bool(link and link.has_attr("top-level"))
        loaders.append(Loader(src=src, cursor=cursor, top_level=top, slot=fp.get("slot")))
    return ParsedPage(comments=rows, loaders=loaders)


def parse_post(html_text: str) -> dict[str, Any]:
    soup = BeautifulSoup(html_text, "html.parser")
    tag = soup.find("shreddit-post")
    if not tag:
        return {}
    post_id = str(tag.get("id") or "")
    text_body = ""
    if post_id:
        body = tag.find(id=f"{post_id}-post-rtjson-content")
        if body:
            text_body = body.get_text("\n", strip=True)
    if not text_body:
        body = tag.find("shreddit-post-text-body")
        if body:
            text_body = body.get_text("\n", strip=True)
    flair = ""
    flair_tag = tag.find("shreddit-post-flair")
    if flair_tag:
        fc = flair_tag.find(class_="flair-content")
        flair = fc.get_text(" ", strip=True) if fc else flair_tag.get_text(" ", strip=True)
    return {
        "post_id": post_id,
        "subreddit": str(tag.get("subreddit-name") or ""),
        "subreddit_id": str(tag.get("subreddit-id") or ""),
        "title": str(tag.get("post-title") or ""),
        "author": str(tag.get("author") or ""),
        "author_id": str(tag.get("author-id") or ""),
        "created": str(tag.get("created-timestamp") or ""),
        "score": _to_int(tag.get("score")),
        "upvote_ratio": _to_float(tag.get("upvote-ratio")),
        "comment_count": _to_int(tag.get("comment-count")),
        "post_type": str(tag.get("post-type") or ""),
        "domain": str(tag.get("domain") or ""),
        "content_url": str(tag.get("content-href") or ""),
        "permalink": str(tag.get("permalink") or ""),
        "flair": flair,
        "text": text_body,
        "logged_in_marker": tag.has_attr("user-logged-in"),
        "nsfw": tag.has_attr("is-nsfw") or tag.has_attr("over-18") or tag.has_attr("nsfw"),
    }
