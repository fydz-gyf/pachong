"""退避与冷却策略。

包含两类：
- 页内退避：请求失败后等待多久重试
- 页间冷却：连续抓若干页后主动休息，降低命中风控的概率
- 跨进程冷却：上一次运行以风控收尾时，本次启动先补足等待
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime

from .checkpoint import load_checkpoint
from .config import Settings
from .errors import is_risk_message
from .utils import parse_iso_datetime


def pick_backoff(ranges: list[tuple[float, float]], failures: int) -> float:
    """按失败次数取退避区间，超出表长则一直用最后一档。"""
    if not ranges:
        return 1.0
    idx = min(max(failures, 1) - 1, len(ranges) - 1)
    low, high = ranges[idx]
    return random.uniform(low, high)


def format_duration(seconds: float) -> str:
    """把秒数显示成人能读的形式：不足 90 秒按秒，否则按分钟。"""
    if seconds < 90:
        return f"{seconds:.0f} 秒"
    return f"{seconds / 60:.1f} 分钟"


def random_sleep(low: float, high: float) -> float:
    seconds = random.uniform(low, high)
    time.sleep(seconds)
    return seconds


def page_cooldown(page_no: int, settings: Settings) -> float:
    """成功抓取一页后的主动休息，返回实际休息秒数。"""
    if page_no % settings.deep_cooldown_every_pages == 0:
        seconds = random_sleep(settings.deep_cooldown_min, settings.deep_cooldown_max)
        logging.info(
            f"已完成 {page_no} 页，进入长任务深度冷却 {seconds:.1f}s"
        )
    elif page_no % settings.cooldown_every_pages == 0:
        seconds = random_sleep(settings.cooldown_min, settings.cooldown_max)
        logging.info(f"已连续完成 {page_no} 页，主动休息 {seconds:.1f}s")
    else:
        seconds = random_sleep(
            settings.request_delay_min, settings.request_delay_max
        )
    return seconds


def find_latest_risk_block(
    keywords: list[str],
    settings: Settings,
) -> tuple[str, datetime] | None:
    """找出这批关键词里最近一次因风控中断的记录。

    返回 (关键词, 断点更新时间)；没有风控记录则返回 None。
    """
    latest: tuple[str, datetime] | None = None

    for keyword in keywords:
        cp = load_checkpoint(keyword, settings)
        if not is_risk_message(cp.get("last_error")):
            continue

        updated = parse_iso_datetime(cp.get("updated_at"))
        if updated is None:
            continue

        if latest is None or updated > latest[1]:
            latest = (keyword, updated)

    return latest


def enforce_cross_process_cooldown(
    keywords: list[str],
    settings: Settings,
) -> float:
    """跨进程风控冷却，返回实际等待秒数。

    进程内的全局熔断只能约束单次运行。用户失败后立刻重跑脚本就是全新进程，
    熔断计时归零，可以紧接着换关键词继续打——这往往正是账号被持续限制的
    原因。这里读取上次断点里的 last_error，如果是风控收尾且间隔不足，
    本次启动先补足冷却。

    只等待"最近一次风控"的差额，避免每个关键词各等一遍。
    """
    latest = find_latest_risk_block(keywords, settings)
    if latest is None:
        return 0.0

    keyword, updated = latest
    elapsed = (datetime.now() - updated).total_seconds()
    need = settings.cross_process_cooldown_min

    if elapsed >= need:
        logging.info(
            f"[{keyword}] 上次风控中断距今已 {format_duration(elapsed)}，无需额外冷却"
        )
        return 0.0

    wait = need - elapsed
    logging.warning(
        f"[{keyword}] 上次运行以淘宝风控收尾，距今仅 {format_duration(elapsed)}。\n"
        f"跨进程冷却：先等待 {format_duration(wait)} 再开始请求，"
        f"避免刚被限制就继续打。\n"
        f"如确需立即开始，可设置 cross_process_cooldown_min=0。"
    )
    time.sleep(wait)
    return wait
