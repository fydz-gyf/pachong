from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass(frozen=True)
class BrowserInfo:
    name: str
    pid: int
    user_data_dir: str
    port: int
    browser: str | None
    browser_ws: str


@dataclass(frozen=True)
class BootstrapData:
    browser: BrowserInfo
    cookies: list[dict[str, Any]]
    csrf_token: str
    client_version: str | None
    csrf_source_path: str | None


@dataclass(frozen=True)
class Loader:
    src: str
    cursor: str
    top_level: bool
    slot: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, obj: dict[str, Any]) -> "Loader":
        return cls(
            src=str(obj.get("src") or ""),
            cursor=str(obj.get("cursor") or ""),
            top_level=bool(obj.get("top_level")),
            slot=(str(obj.get("slot")) if obj.get("slot") is not None else None),
        )


@dataclass
class FetchResult:
    status_code: int
    text: str
    content_type: str
    final_url: str
    rate_used: float | None
    rate_remaining: float | None
    rate_reset: float | None


@dataclass
class ParsedPage:
    comments: list[dict[str, Any]]
    loaders: list[Loader]


@dataclass
class MediaItem:
    """从帖子页 HTML 里解析出来的一个媒体资源。"""

    kind: str  # image | video | external
    url: str  # 归一化后的直链
    source: str  # 提取来源，便于排查漏抓
    poster: str = ""  # 视频封面（下载失败时的兜底图）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
