"""淘宝 MTop HTTP 抓取器。

原单体脚本 taobao_scraper.py 已拆分为下列模块：

    config       可调参数与目录（Settings）
    errors       异常类型与风控文案识别
    utils        通用小工具
    logging_setup 日志初始化
    cdp          Chrome DevTools Protocol 客户端
    auth         登录态持久化 / MTop 签名
    mtop         搜索接口请求、错误分类与重试
    extract      响应解析
    checkpoint   断点续抓
    cooling      退避与冷却策略
    scraper      单关键词分页抓取
    images       主图下载与压缩
    excel        Excel 导出
    cli          命令行与交互配置
    runner       任务编排

典型用法：

    from taobao_scraper import Settings, run

    settings = Settings(keywords=["accent chair"], max_pages=5)
    run(settings)
"""

from __future__ import annotations

import logging
import os
import sys

from .config import Settings

__all__ = ["Settings", "run", "main"]

__version__ = "2.0.0"


def run(settings: Settings) -> int:
    """执行一次完整抓取任务，返回退出码。"""
    from .runner import run as _run

    return _run(settings)


def main(argv: list[str] | None = None) -> int:
    """命令行入口。返回进程退出码。"""
    from .cli import settings_from_cli
    from .logging_setup import setup_logging

    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    setup_logging()
    settings = settings_from_cli(Settings(), argv)

    try:
        return run(settings)
    except KeyboardInterrupt:
        print("\n用户中止。已完成页的断点会保留，Excel 中已导出部分结果。")
        return 130
    except Exception as e:
        logging.exception(f"执行失败: {e}")
        print("\n执行失败：", e)
        return 1
