from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Paths:
    root: Path
    runtime: Path = field(init=False)
    checkpoints: Path = field(init=False)
    raw: Path = field(init=False)
    next_data: Path = field(init=False)
    images: Path = field(init=False)
    output: Path = field(init=False)
    auth_state: Path = field(init=False)

    def __post_init__(self):
        self.root = self.root.resolve()
        self.runtime = self.root / "runtime"
        self.checkpoints = self.runtime / "checkpoints"
        self.raw = self.runtime / "raw"
        self.next_data = self.runtime / "next_data"
        self.images = self.runtime / "image_cache"
        self.output = self.root / "output"
        self.auth_state = self.runtime / "walmart_auth_state.json"
        for path in (self.runtime, self.checkpoints, self.raw, self.next_data, self.images, self.output):
            path.mkdir(parents=True, exist_ok=True)


@dataclass
class Settings:
    keywords: list[str] = field(default_factory=lambda: ["office chair"])
    max_pages: int = 5
    resume: bool = True
    force_refresh: bool = False
    save_html: bool = False
    save_next_data: bool = False
    filter_sponsored: bool = False
    embed_images: bool = True
    image_workers: int = 5
    image_max_size: int = 120
    image_jpeg_quality: int = 80
    keep_image_cache: bool = False
    # Sorftime enrichment. HTTP mode reads the logged-in extension token once from
    # AdsPower, then calls Sorftime directly without loading/scrolling every page.
    sorftime_enabled: bool = True
    sorftime_mode: str = "http"  # http | browser
    sorftime_browser_fallback: bool = True
    sorftime_wait_timeout: int = 18
    sorftime_nmversion: int = 110
    sorftime_extension_id: str = "aadiiicebnjmjmibjengdohedcfeekeg"
    # Prefer SORFTIME_TOKEN env var over putting credentials in config/code.
    sorftime_token: str = ""
    request_timeout: int = 45
    max_request_retries: int = 4
    request_delay_min: float = 2.5
    request_delay_max: float = 4.5
    max_empty_pages: int = 2
    block_backoff_ranges: tuple[tuple[int, int], ...] = ((10, 18), (25, 40), (60, 90), (120, 180))
    network_backoff_ranges: tuple[tuple[int, int], ...] = ((2, 4), (5, 9), (12, 20), (25, 40))
    impersonate: str = "chrome"
    proxy_url: str = ""
    cdp_host: str = "127.0.0.1"
    cdp_port: int = 9222
    use_cdp_fallback: bool = True
    # If a standalone HTTP request is challenged, optionally load the same URL in the
    # already-open browser profile and parse that visible page. Human verification is
    # never solved automatically; the browser fallback only reuses the user-controlled tab.
    browser_page_fallback: bool = True
    # Browser authentication source: auto | chrome | adspower
    browser_mode: str = "auto"
    adspower_api_base: str = "http://local.adspower.net:50325"
    adspower_api_key_env: str = "ADSPOWER_API_KEY"
    adspower_api_timeout: int = 8
    adspower_profile_id: str = ""
    adspower_profile_no: str = ""
    adspower_auto_start: bool = True
    adspower_sync_proxy: bool = True
    # Free-plan fallback: discover the CDP port from a running AdsPower Chromium process.
    adspower_process_discovery: bool = True
    # Optional explicit AdsPower Chromium CDP port. 0 = auto-discover.
    adspower_cdp_port: int = 0
    paths: Paths | None = None

    def bind_root(self, root: Path) -> "Settings":
        self.paths = Paths(root)
        return self
