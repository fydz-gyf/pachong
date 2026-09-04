from __future__ import annotations

import html as html_lib
import re
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlsplit

from bs4 import BeautifulSoup, Tag

from reddit_scraper.models import MediaItem

MEDIA_HOST_SUFFIX = (
    "i.redd.it",
    "preview.redd.it",
    "external-preview.redd.it",
    "v.redd.it",
    "i.imgur.com",
)
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp")
VIDEO_EXT = (".mp4", ".webm", ".mov", ".m4v")
MANIFEST_EXT = (".mpd", ".m3u8")
NOISE_MARKERS = ("/avatars/", "redditstatic.com", "styles.redditmedia.com", "emoji/", "/static/")

_V_REDDIT = re.compile(r"^https?://(?:[a-z0-9-]+\.)?v\.redd\.it/([A-Za-z0-9_-]+)", re.I)


def _host(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower()
    except Exception:
        return ""


def _is_media_host(url: str) -> bool:
    host = _host(url)
    return any(host == h or host.endswith("." + h) for h in MEDIA_HOST_SUFFIX)


def _is_noise(url: str) -> bool:
    low = url.lower()
    return any(marker in low for marker in NOISE_MARKERS)


def normalize_url(url: Any) -> str:
    raw = html_lib.unescape(str(url or "")).strip()
    if not raw:
        return ""
    raw = raw.split("#", 1)[0].strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    return raw


def kind_from_url(url: str) -> str:
    parts = urlsplit(normalize_url(url))
    path = parts.path.lower()
    query = {k.lower(): v.lower() for k, v in parse_qsl(parts.query, keep_blank_values=True)}
    fmt = query.get("format", "")
    if fmt in {"mp4", "webm", "mov", "m4v"}:
        return "video"
    if fmt in {"jpg", "jpeg", "png", "webp", "gif", "avif", "bmp"}:
        return "image"
    if path.endswith(MANIFEST_EXT) or path.endswith(VIDEO_EXT):
        return "video"
    if path.endswith(IMAGE_EXT):
        return "image"
    if "v.redd.it" in _host(url):
        return "video"
    return "external"


def video_id(url: str) -> str:
    m = _V_REDDIT.match(normalize_url(url))
    return m.group(1) if m else ""


def _dedupe_key(url: str) -> str:
    parts = urlsplit(normalize_url(url))
    host = parts.netloc.lower()
    path = parts.path
    if host in {"i.redd.it", "preview.redd.it", "external-preview.redd.it"}:
        return path
    return normalize_url(url)


def _add(bucket: list[MediaItem], seen: set[str], url: Any, source: str, poster: str = "") -> None:
    item_url = normalize_url(url)
    if not item_url.startswith("http") or _is_noise(item_url):
        return
    key = _dedupe_key(item_url)
    if key in seen:
        return
    seen.add(key)
    bucket.append(MediaItem(kind=kind_from_url(item_url), url=item_url, source=source, poster=normalize_url(poster)))


def _attr_values(root: Tag | BeautifulSoup, tags: Iterable[str], attrs: Iterable[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for tag_name in tags:
        for node in root.find_all(tag_name):
            for attr in attrs:
                value = node.get(attr)
                if isinstance(value, str) and value.strip():
                    out.append((tag_name, value.strip()))
    return out


def _meta_values(soup: BeautifulSoup, keys: Iterable[str]) -> list[str]:
    wanted = {str(x).lower() for x in keys}
    out: list[str] = []
    for meta in soup.find_all("meta"):
        key = str(meta.get("property") or meta.get("name") or "").lower()
        if key in wanted:
            content = meta.get("content")
            if isinstance(content, str) and content.strip():
                out.append(content.strip())
    return out


def _target_post(soup: BeautifulSoup) -> Tag | None:
    posts = list(soup.find_all("shreddit-post"))
    if not posts:
        return None

    def score(tag: Tag) -> int:
        n = 0
        if str(tag.get("view-context") or "").lower() == "commentspage":
            n += 100
        if tag.has_attr("data-expected-lcp"):
            n += 25
        if str(tag.get("id") or "").startswith("t3_"):
            n += 10
        if tag.get("post-title"):
            n += 5
        if tag.get("permalink") and "/comments/" in str(tag.get("permalink")):
            n += 5
        return n

    return max(posts, key=score)


def _best_image_url(node: Tag) -> str:
    candidates: list[tuple[int, str]] = []
    for attr in ("srcset", "data-srcset"):
        raw = node.get(attr)
        if not isinstance(raw, str):
            continue
        for part in raw.split(","):
            bits = part.strip().rsplit(" ", 1)
            url = bits[0].strip()
            width = 0
            if len(bits) == 2 and bits[1].lower().endswith("w"):
                try:
                    width = int(bits[1][:-1])
                except Exception:
                    width = 0
            if url:
                candidates.append((width, url))
    for attr in ("data-src", "src"):
        raw = node.get(attr)
        if isinstance(raw, str) and raw.strip():
            candidates.append((1, raw.strip()))
    if not candidates:
        return ""
    candidates.sort(key=lambda x: x[0], reverse=True)
    return normalize_url(candidates[0][1])


def _video_poster(post: Tag, soup: BeautifulSoup) -> str:
    for name in ("video", "shreddit-player", "shreddit-video"):
        for node in post.find_all(name):
            for attr in ("poster", "poster-url", "data-poster"):
                value = node.get(attr)
                if isinstance(value, str) and value.strip() and _is_media_host(value):
                    return normalize_url(value)
    for name in ("faceplate-img", "img"):
        node = post.find(name)
        if isinstance(node, Tag):
            url = _best_image_url(node)
            if url and _is_media_host(url) and not _is_noise(url):
                return url
    for value in _meta_values(soup, ("og:image", "og:image:url", "twitter:image")):
        if _is_media_host(value) and not _is_noise(value):
            return normalize_url(value)
    return ""


def extract_media(html_text: str) -> list[MediaItem]:
    # Extract original-post media only.
    soup = BeautifulSoup(html_text or "", "html.parser")
    items: list[MediaItem] = []
    seen: set[str] = set()
    seen_paths: set[tuple[str, str]] = set()

    post_tag = soup.find("shreddit-post")
    if post_tag is None:
        return []

    post_type = str(post_tag.get("post-type") or "").strip().lower()

    def canonical_key(url: str) -> tuple[str, str]:
        u = normalize_url(url)
        try:
            p = urlsplit(u)
            return (p.netloc.lower(), p.path)
        except Exception:
            return ("", u)

    def add_owned(url: Any, source: str, poster: str = "") -> bool:
        u = normalize_url(url)
        if not u.startswith("http"):
            return False
        if not _is_media_host(u):
            return False
        k = canonical_key(u)
        if k in seen_paths:
            return False
        before = len(items)
        _add(items, seen, u, source, poster=poster)
        if len(items) > before:
            seen_paths.add(k)
            return True
        return False

    def candidate_urls(node: Tag) -> list[str]:
        out: list[str] = []
        for attr in (
            "src", "data-src", "href", "content-href",
            "data-url", "data-media-url", "poster",
        ):
            value = node.get(attr)
            if isinstance(value, str) and value.strip():
                out.append(value.strip())

        for attr in ("srcset", "data-srcset"):
            value = node.get(attr)
            if not isinstance(value, str) or not value.strip():
                continue
            best_url = ""
            best_weight = -1.0
            for part in value.split(","):
                bits = part.strip().split()
                if not bits:
                    continue
                weight = 0.0
                if len(bits) > 1:
                    desc = bits[-1].lower()
                    try:
                        if desc.endswith("w"):
                            weight = float(desc[:-1])
                        elif desc.endswith("x"):
                            weight = float(desc[:-1]) * 10000.0
                    except Exception:
                        weight = 0.0
                if weight >= best_weight:
                    best_weight = weight
                    best_url = bits[0]
            if best_url:
                out.insert(0, best_url)

        return out

    def nearest_comment(node: Tag):
        try:
            return node.find_parent("shreddit-comment")
        except Exception:
            return None

    if post_type == "video":
        # Keep the poster alongside the canonical video URL.  The helper is
        # intentionally scoped to the target post so page-level thumbnails do
        # not leak into a post's media record.
        poster = _video_poster(post_tag, soup)
        for node in post_tag.find_all(
            [
                "video", "source", "shreddit-player", "shreddit-video",
                "shreddit-player-2", "shreddit-player-static",
                "shreddit-player-static-hlsjs",
            ]
        ):
            for value in candidate_urls(node):
                u = normalize_url(value)
                if kind_from_url(u) == "video" or "v.redd.it" in _host(u):
                    add_owned(u, f"post.{node.name}", poster=poster)

        content_href = post_tag.get("content-href")
        if isinstance(content_href, str):
            u = normalize_url(content_href)
            if "v.redd.it" in _host(u):
                add_owned(u, "post.content-href", poster=poster)

        manifests = [x for x in items if x.url.lower().endswith(MANIFEST_EXT)]
        if manifests:
            return manifests[:1]
        videos = [x for x in items if x.kind == "video"]
        return videos[:1]

    first_post_image = ""

    content_href = post_tag.get("content-href")
    if isinstance(content_href, str):
        u = normalize_url(content_href)
        if _is_media_host(u) and kind_from_url(u) == "image":
            first_post_image = u
            add_owned(u, "post.content-href")

    if not first_post_image:
        for node in post_tag.find_all(["faceplate-img", "img", "source"]):
            for value in candidate_urls(node):
                u = normalize_url(value)
                if _is_media_host(u) and kind_from_url(u) == "image":
                    first_post_image = u
                    add_owned(u, "post.img")
                    break
            if first_post_image:
                break

    if post_type != "gallery":
        return [x for x in items if x.kind == "image"]

    gallery_prefix = ""

    if first_post_image:
        filename = urlsplit(first_post_image).path.rsplit("/", 1)[-1]
        low = filename.lower()
        if "-v0-" in low:
            gallery_prefix = filename[: low.index("-v0-") + 4]

    if gallery_prefix:
        # DOM fallback: same gallery prefix, but only outside comments.
        for node in soup.find_all(["faceplate-img", "img", "source", "a"]):
            if nearest_comment(node) is not None:
                continue
            for value in candidate_urls(node):
                u = normalize_url(value)
                if not _is_media_host(u) or kind_from_url(u) != "image":
                    continue
                filename = urlsplit(u).path.rsplit("/", 1)[-1]
                if filename.lower().startswith(gallery_prefix.lower()):
                    add_owned(u, "post.gallery.non-comment-dom")

        # SSR/lazy fallback: same gallery prefix, but only before comment tree.
        raw_before_comments = html_text or ""
        lower_raw = raw_before_comments.lower()
        cut_points = [
            p for p in (
                lower_raw.find("<shreddit-comment-tree"),
                lower_raw.find("<shreddit-comment "),
            )
            if p >= 0
        ]
        if cut_points:
            raw_before_comments = raw_before_comments[: min(cut_points)]

        escaped_prefix = re.escape(gallery_prefix)
        pattern = re.compile(
            r"https?://(?:preview\.redd\.it|i\.redd\.it)/"
            + escaped_prefix
            + r"[^\"'<>\s]+",
            re.I,
        )

        for raw_url in pattern.findall(raw_before_comments):
            u = normalize_url(raw_url)
            if kind_from_url(u) == "image":
                add_owned(u, "post.gallery.pre-comment-ssr")

    return [x for x in items if x.kind == "image"]


def extract_comment_media(tag: Tag) -> list[MediaItem]:
    # Extract media owned by this exact comment only.
    items: list[MediaItem] = []
    seen: set[str] = set()
    seen_paths: set[tuple[str, str]] = set()

    def canonical_key(url: str) -> tuple[str, str]:
        u = normalize_url(url)
        try:
            p = urlsplit(u)
            return (p.netloc.lower(), p.path)
        except Exception:
            return ("", u)

    def owned_by_this_comment(node: Tag) -> bool:
        try:
            return node.find_parent("shreddit-comment") is tag
        except Exception:
            return False

    def add_comment(url: Any, source: str):
        u = normalize_url(url)
        if not u.startswith("http") or not _is_media_host(u):
            return
        k = canonical_key(u)
        if k in seen_paths:
            return
        before = len(items)
        _add(items, seen, u, source)
        if len(items) > before:
            seen_paths.add(k)

    for node in tag.find_all(
        ["faceplate-img", "img", "video", "source", "a"]
    ):
        if not owned_by_this_comment(node):
            continue

        for attr in (
            "src", "data-src", "href", "data-url",
            "data-media-url", "poster",
        ):
            value = node.get(attr)
            if isinstance(value, str) and value.startswith("http"):
                add_comment(value, f"comment.direct.{node.name}")

        for attr in ("srcset", "data-srcset"):
            value = node.get(attr)
            if not isinstance(value, str):
                continue
            for part in value.split(","):
                bits = part.strip().split()
                if bits and bits[0].startswith("http"):
                    add_comment(bits[0], f"comment.direct.{node.name}.srcset")

    return items
