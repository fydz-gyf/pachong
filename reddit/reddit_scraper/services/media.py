from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from reddit_scraper.models import MediaItem
from reddit_scraper.parser.media import normalize_url, video_id

MEDIA_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "download_images": True,
    "download_videos": True,
    "download_comment_media": False,
    "max_items_per_post": 50,
    "max_file_size_mb": 200,
    "media_network_retries": 1,
    "max_requests_per_post": 80,
    "ffmpeg_mux": True,
    "ffmpeg_timeout_seconds": 300,
    "dir_name": "media",
}

_DASH_HEIGHTS = (1080, 720, 480, 360, 240)
_EXT_BY_CTYPE = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/gif": ".gif", "image/avif": ".avif",
    "video/mp4": ".mp4", "video/webm": ".webm", "audio/mp4": ".m4a",
    "audio/aac": ".aac",
}


def _safe_stem(value: str, fallback: str) -> str:
    keep = [c for c in value if c.isalnum() or c in "-_"]
    return "".join(keep)[:60] or fallback


def _ext_from(url: str, content_type: str) -> str:
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype in _EXT_BY_CTYPE:
        return _EXT_BY_CTYPE[ctype]
    path = urlsplit(url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".mp4", ".webm", ".m4a", ".aac"):
        if path.endswith(ext):
            return ext
    return ".bin"


def _kb(size: int) -> float:
    return round(size / 1024, 1)


class MediaDownloader:
    """Download post/comment media without letting media failures break comment scraping."""

    def __init__(self, client, media_root: Path, config: dict[str, Any] | None = None):
        self.client = client
        self.root = Path(media_root)
        self.cfg = dict(MEDIA_DEFAULTS)
        self.cfg.update(config or {})
        self.ffmpeg = shutil.which("ffmpeg") if self.cfg.get("ffmpeg_mux") else None
        self.max_bytes = int(float(self.cfg.get("max_file_size_mb", 200)) * 1024 * 1024)
        self.request_count = 0
        self.max_requests = max(0, int(self.cfg.get("max_requests_per_post", 80)))

    def download_post(
        self, post_id: str, items: list[MediaItem], referer: str = ""
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        records, summary = self._download_all(post_id, items or [], referer, scope="post")
        for r in records:
            r["post_id"] = post_id
            r["media_scope"] = "post"
            r["comment_id"] = ""
        return records, summary

    def download_comment_media(
        self, post_id: str, comment_id: str, items: list[MediaItem], referer: str = ""
    ) -> list[dict[str, Any]]:
        prefix = f"{post_id}_{_safe_stem(comment_id, 'comment')}"
        records, _ = self._download_all(prefix, items or [], referer, scope="comment")
        for r in records:
            r["post_id"] = post_id
            r["media_scope"] = "comment"
            r["comment_id"] = comment_id
        return records

    def _download_all(
        self, prefix: str, items: list[MediaItem], referer: str, scope: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not self.cfg.get("enabled"):
            return records, {
                "media_type": "none", "media_count": 0,
                "media_dir": "", "media_note": "媒体功能已关闭",
            }
        if not items:
            return records, {
                "media_type": "none", "media_count": 0,
                "media_dir": "", "media_note": "未解析到媒体",
            }

        limit = max(0, int(self.cfg.get("max_items_per_post", 50)))
        selected = items[:limit] if limit else items
        skipped = len(items) - len(selected)

        kinds: list[str] = []

        def stamp(record: dict[str, Any]) -> dict[str, Any]:
            record["media_scope"] = scope
            record.setdefault("comment_id", "")
            return record

        for index, item in enumerate(selected, 1):
            if item.kind == "video":
                record = self._handle_video(prefix, index, item, referer)
            elif item.kind == "image":
                record = self._handle_image(prefix, index, item, referer)
            else:
                record = self._record(
                    prefix, index, item, None, 0,
                    "未下载", "外链媒体，仅记录链接",
                )
            record = stamp(record)
            records.append(record)
            if record.get("status") in {"成功", "已存在", "链接"}:
                kinds.append(str(record.get("kind") or item.kind))

        for offset, item in enumerate(items[len(selected):], len(selected) + 1):
            records.append(stamp(self._record(
                prefix, offset, item, None, 0,
                "未下载", f"超出单帖上限 {limit}",
            )))

        available = [
            r for r in records
            if r.get("status") in {"成功", "已存在", "链接"}
        ]
        has_local = any(str(r.get("path") or "").strip() for r in available)

        notes: list[str] = []
        if any(r.get("kind") == "video" and r.get("status") == "链接" for r in records):
            notes.append("视频仅保留链接，不下载")
        if skipped > 0:
            notes.append(f"跳过 {skipped} 个（超出上限）")

        return records, {
            "media_type": "+".join(dict.fromkeys(kinds)) if kinds else "none",
            "media_count": len(available),
            "media_dir": str(self.root) if has_local else "",
            "media_note": "；".join(notes),
        }

    def _record(
        self, prefix: str, index: int, item: MediaItem, file_path: Path | None, size: int,
        status: str, note: str = "", kind: str | None = None,
    ) -> dict[str, Any]:
        return {
            "post_id": prefix,
            "index": index,
            "kind": kind or item.kind,
            "source": item.source,
            "url": item.url,
            "filename": file_path.name if file_path else "",
            "path": str(file_path) if file_path else "",
            "size_kb": _kb(size) if size else "",
            "status": status,
            "note": note,
        }

    def _target(self, prefix: str, index: int, ext: str, role: str = "") -> Path:
        name = f"{prefix}_{index:02d}{('_' + role) if role else ''}{ext}"
        return self.root / name

    def _existing_variant(self, target: Path, min_bytes: int) -> Path | None:
        if target.exists() and target.stat().st_size >= min_bytes:
            return target
        if not target.parent.exists():
            return None
        for candidate in target.parent.glob(target.stem + ".*"):
            if candidate.suffix.lower() == ".part" or candidate.name.endswith(".part"):
                continue
            try:
                if candidate.is_file() and candidate.stat().st_size >= min_bytes:
                    return candidate
            except OSError:
                continue
        return None

    def _budget_ok(self) -> bool:
        return self.max_requests <= 0 or self.request_count < self.max_requests

    def _save(self, url: str, referer: str, target: Path, min_bytes: int = 512) -> tuple[Path | None, int, str]:
        existing = self._existing_variant(target, min_bytes)
        if existing is not None:
            return existing, existing.stat().st_size, "已存在"
        if not self._budget_ok():
            return None, 0, f"媒体请求预算已用完({self.max_requests})"

        self.root.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        part.unlink(missing_ok=True)
        self.request_count += 1

        if hasattr(self.client, "fetch_media_to_file"):
            try:
                status, ctype, flag, size = self.client.fetch_media_to_file(
                    url,
                    part,
                    referer=referer,
                    max_bytes=self.max_bytes,
                    retries=max(0, int(self.cfg.get("media_network_retries", 1))),
                )
            except Exception as exc:
                part.unlink(missing_ok=True)
                return None, 0, f"请求异常: {type(exc).__name__}: {exc}"
            if flag == "TOO_LARGE":
                part.unlink(missing_ok=True)
                return None, 0, f"超过大小上限 {self.cfg.get('max_file_size_mb')}MB"
            if flag == "HTML_WRAPPER":
                part.unlink(missing_ok=True)
                return None, 0, "返回 HTML 页面，非媒体直链"
            if status != 200 or flag != "OK" or size < min_bytes:
                part.unlink(missing_ok=True)
                detail = flag if flag and flag != "OK" else f"HTTP {status}"
                return None, size, detail
            final = target.with_suffix(_ext_from(url, ctype))
            final.unlink(missing_ok=True)
            part.replace(final)
            return final, size, "成功"

        # Compatibility fallback for an older client implementation.
        try:
            status, payload, ctype, flag = self.client.fetch_media(
                url,
                referer=referer,
                max_bytes=self.max_bytes,
                retries=max(0, int(self.cfg.get("media_network_retries", 1))),
            )
        except Exception as exc:
            return None, 0, f"请求异常: {type(exc).__name__}: {exc}"
        if flag == "TOO_LARGE":
            return None, 0, f"超过大小上限 {self.cfg.get('max_file_size_mb')}MB"
        if flag == "HTML_WRAPPER":
            return None, 0, "返回 HTML 页面，非媒体直链"
        if status != 200 or not payload:
            return None, 0, flag or f"HTTP {status}"
        if len(payload) < min_bytes:
            return None, len(payload), f"响应过小({len(payload)}B)"
        final = target.with_suffix(_ext_from(url, ctype))
        final.write_bytes(payload)
        return final, len(payload), "成功"

    def _handle_image(self, prefix: str, index: int, item: MediaItem, referer: str) -> dict[str, Any]:
        if not self.cfg.get("download_images"):
            return self._record(prefix, index, item, None, 0, "未下载", "图片下载已关闭")
        target = self._target(prefix, index, _ext_from(item.url, ""))
        path, size, note = self._save(item.url, referer, target)
        if path is not None and note in {"成功", "已存在"}:
            return self._record(prefix, index, item, path, size, "成功", note if note == "已存在" else "")
        return self._record(prefix, index, item, None, size, "失败", note)

    def _video_candidates(self, item: MediaItem) -> tuple[list[str], list[str]]:
        url = normalize_url(item.url)
        vid = video_id(url)
        path = urlsplit(url).path.lower()
        manifest = path.endswith((".mpd", ".m3u8"))
        direct_video = path.endswith((".mp4", ".webm", ".mov", ".m4v"))

        videos: list[str] = []
        audios: list[str] = []
        if direct_video:
            videos.append(url)
        if vid:
            base = f"https://v.redd.it/{vid}/"
            for height in _DASH_HEIGHTS:
                candidate = base + f"DASH_{height}.mp4"
                if candidate not in videos:
                    videos.append(candidate)
            audios.extend([base + "DASH_AUDIO_128.mp4", base + "DASH_audio.mp4"])
        elif not manifest and url:
            videos.append(url)
        return videos, audios

    def _handle_video(
        self, prefix: str, index: int, item: MediaItem, referer: str
    ) -> dict[str, Any]:
        vid = video_id(item.url)
        link = f"https://v.redd.it/{vid}" if vid else normalize_url(item.url)
        link_item = MediaItem(
            kind="video",
            url=link,
            source=item.source,
            poster=item.poster,
        )
        return self._record(
            prefix, index, link_item, None, 0,
            "链接", "视频仅保留链接，不下载", "video",
        )

    def _run_ffmpeg(self, args: list[str]) -> tuple[bool, str]:
        if not self.ffmpeg:
            return False, "ffmpeg not found"
        try:
            proc = subprocess.run(
                [self.ffmpeg, "-y", "-loglevel", "error", *args],
                capture_output=True,
                timeout=float(self.cfg.get("ffmpeg_timeout_seconds", 300)),
            )
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if proc.returncode == 0:
            return True, ""
        return False, (proc.stderr or b"").decode("utf-8", "ignore").strip()[-300:]
