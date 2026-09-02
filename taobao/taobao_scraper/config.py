"""全局配置。

拆分后不再使用模块级全局变量，所有可调参数集中在 Settings 里显式传递，
这样各模块之间没有隐式依赖，也方便测试时注入不同配置。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ============================================================
# MTop 已验证参数
# ============================================================

APP_KEY = "12574478"
API_NAME = "mtop.relationrecommend.wirelessrecommend.recommend"
API_VERSION = "2.0"
API_URL = (
    "https://h5api.m.taobao.com/h5/"
    "mtop.relationrecommend.wirelessrecommend.recommend/2.0/"
)
APP_ID = "34385"

RUNTIME_DIRNAME = "taobao_runtime"
OUTPUT_DIRNAME = "taobao_output"
AUTH_STATE_FILENAME = "taobao_auth_state.json"

# 避免系统代理影响本机 CDP 连接
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

# 包位于 <项目根>/taobao_scraper/，因此项目根是上一层
_PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_BASE_DIR = _PACKAGE_DIR.parent


def _default_keywords() -> list[str]:
    return ["accent chair", "coffee table"]


def initial_paging_state() -> dict:
    """MTop 分页初始状态。

    放在 config 而不是 mtop，是为了让 checkpoint 能直接引用它，
    避免 checkpoint → mtop → cooling → checkpoint 的循环导入。
    """
    return {
        "sourceS": "0",
        "bcoffset": "",
        "ntoffset": "",
        "totalPage": "100",
        "totalResults": "4800",
        "sessionid": "",
    }


@dataclass
class Settings:
    """一次抓取任务的全部可调参数。"""

    # ---------------- 任务范围 ----------------
    keywords: list[str] = field(default_factory=_default_keywords)
    max_pages: int = 10
    resume: bool = True
    force_refresh: bool = False
    save_raw_response: bool = False
    filter_ads: bool = False

    # ---------------- Excel 输出 ----------------
    embed_images: bool = True
    export_video: bool = True
    excel_append_timestamp: bool = False

    # ---------------- 图片 ----------------
    # 图片 CDN 对瞬时并发比较敏感，12 线程容易触发 TLS EOF。
    image_workers: int = 5
    image_max_size: int = 120
    image_jpeg_quality: int = 76
    keep_image_cache: bool = False
    image_download_retries: int = 4
    image_connect_timeout: int = 8
    image_read_timeout: int = 25
    image_retry_base_delay: float = 1.0
    # 单张主图的总时间预算。候选 CDN 最多 6 个、每个最多重试 4 次，
    # 全部跑满最坏要 100s+，会把线程池拖死。超预算直接放弃，换下一张。
    image_total_timeout: float = 40.0

    # ---------------- 请求节奏 ----------------
    request_delay_min: float = 1.20
    request_delay_max: float = 2.20
    request_timeout: int = 20
    max_request_retries: int = 5

    # ---------------- 退避区间 ----------------
    # 淘宝返回 FAIL_SYS_USER_VALIDATE / RGV587 时不要立即狂重试，
    # 这是风控/频率验证，不是普通 token 过期。
    risk_backoff_ranges: list[tuple[float, float]] = field(
        default_factory=lambda: [(8, 14), (18, 28), (35, 55), (65, 95)]
    )
    # MTop 网络/TLS 临时异常（SSLEOFError / ConnectionError / ReadTimeout）
    network_backoff_ranges: list[tuple[float, float]] = field(
        default_factory=lambda: [(2, 4), (5, 8), (10, 16), (20, 30)]
    )
    # 淘宝 MTop/TPP 后端自身临时超时（SOLUTION_EXECUTE_TIMEOUT）
    server_backoff_ranges: list[tuple[float, float]] = field(
        default_factory=lambda: [(2, 5), (5, 10), (10, 18), (20, 35)]
    )

    # ---------------- 主动冷却 ----------------
    cooldown_every_pages: int = 4
    cooldown_min: float = 6.0
    cooldown_max: float = 10.0

    deep_cooldown_every_pages: int = 15
    deep_cooldown_min: float = 35.0
    deep_cooldown_max: float = 60.0

    # 某一页连续重试耗尽风控后，先全局熔断，再从断点原页重试。
    global_risk_resume_cycles: int = 1
    global_risk_cooldown_min: float = 300.0
    global_risk_cooldown_max: float = 480.0

    # 跨进程风控冷却。
    # 上一次运行若以风控收尾，本次启动会先补足这段等待，
    # 否则"失败后立刻重跑换关键词"会让进程内熔断完全失效。
    cross_process_cooldown_min: float = 300.0

    # 切换关键词前的基础冷却
    risk_switch_cooldown_min: float = 120.0
    risk_switch_cooldown_max: float = 180.0
    normal_switch_cooldown_min: float = 20.0
    normal_switch_cooldown_max: float = 35.0

    # ---------------- 抓取终止条件 ----------------
    max_empty_pages: int = 2

    # ---------------- Chrome CDP ----------------
    cdp_host: str = "127.0.0.1"
    cdp_port: int = 9222
    use_cdp_fallback: bool = True

    # ---------------- 目录 ----------------
    base_dir: Path = DEFAULT_BASE_DIR

    def __post_init__(self) -> None:
        self.base_dir = Path(self.base_dir)
        # keywords 允许传入单个字符串
        if isinstance(self.keywords, str):
            self.keywords = [self.keywords]

    # ---------------- 派生路径 ----------------
    @property
    def runtime_dir(self) -> Path:
        return self.base_dir / RUNTIME_DIRNAME

    @property
    def checkpoint_dir(self) -> Path:
        return self.runtime_dir / "checkpoints"

    @property
    def raw_dir(self) -> Path:
        return self.runtime_dir / "raw"

    @property
    def image_cache_dir(self) -> Path:
        return self.runtime_dir / "image_cache"

    @property
    def auth_state_file(self) -> Path:
        return self.runtime_dir / AUTH_STATE_FILENAME

    @property
    def output_dir(self) -> Path:
        return self.base_dir / OUTPUT_DIRNAME

    def ensure_dirs(self) -> None:
        """创建运行时目录。放在显式调用里，避免 import 时产生副作用。"""
        for path in (
            self.runtime_dir,
            self.checkpoint_dir,
            self.raw_dir,
            self.image_cache_dir,
            self.output_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
