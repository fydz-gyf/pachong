"""日志初始化。

从原脚本的顶层 logging.basicConfig 抽出来，改为显式调用，
避免 import 包时就修改全局 logging 状态。
"""

from __future__ import annotations

import logging


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    # openpyxl 在写大表时会刷大量 DEBUG，压制掉
    logging.getLogger("openpyxl").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
