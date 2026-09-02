"""任务编排：登录 -> 跨进程冷却 -> 逐关键词抓取 -> 导出 Excel。

导出放在 finally 里，保证中途 Ctrl+C 或单关键词异常时，
已经抓到的数据仍然能落盘成 Excel（数据在断点里不丢，但用户需要 xlsx）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from . import auth, cooling
from .checkpoint import load_checkpoint, visible_products
from .config import Settings
from .errors import is_risk_message
from .excel import export_excel
from .logging_setup import setup_logging
from .mtop import reset_requests_connection_pool
from .scraper import scrape_keyword


def _mark_incomplete(incomplete: list[str], keyword: str) -> None:
    if keyword not in incomplete:
        incomplete.append(keyword)


def _scrape_with_risk_circuit(
    session: requests.Session,
    environment: dict,
    keyword: str,
    settings: Settings,
) -> tuple[list[dict], dict, bool, bool]:
    """抓取一个关键词，必要时进入全局熔断后从断点原页继续。

    返回 (商品列表, environment, 是否完成, 是否被风控阻断)。
    """
    products: list[dict] = []
    completed = False
    risk_resume_cycle = 0
    is_risk_block = False

    while True:
        products, environment, completed = scrape_keyword(
            session=session,
            environment=environment,
            keyword=keyword,
            settings=settings,
        )

        if completed:
            break

        cp = load_checkpoint(keyword, settings)
        is_risk_block = is_risk_message(cp.get("last_error"))

        if is_risk_block and risk_resume_cycle < settings.global_risk_resume_cycles:
            risk_resume_cycle += 1
            pause = cooling.random_sleep(
                settings.global_risk_cooldown_min,
                settings.global_risk_cooldown_max,
            )
            logging.warning(
                f"[{keyword}] 当前不是单页偶发失败，而是持续风控。"
                f"已全局熔断冷却 {pause/60:.1f} 分钟；"
                f"冷却后从断点第 {cp.get('next_page')} 页继续，"
                f"不会切换关键词，也不会刷新 Cookie。"
            )
            reset_requests_connection_pool(session)
            continue

        if is_risk_block:
            logging.error(
                f"[{keyword}] 全局冷却后仍持续触发淘宝访问验证。"
                f"为避免继续加重限制，本次任务停止继续请求该关键词；"
                f"断点保留在第 {cp.get('next_page')} 页。"
            )
            # 风控通常作用于当前会话/IP，短暂切换关键词意义不大。
            # 因此不再只休息十几秒就继续打下一个关键词。
            pause = cooling.random_sleep(
                settings.risk_switch_cooldown_min,
                settings.risk_switch_cooldown_max,
            )
            logging.warning(f"切换下一个关键词前额外冷却 {pause/60:.1f} 分钟")
        else:
            pause = cooling.random_sleep(
                settings.normal_switch_cooldown_min,
                settings.normal_switch_cooldown_max,
            )
            logging.warning(
                f"[{keyword}] 未完成全部目标页；切换下一个关键词前休息 {pause:.1f}s"
            )
        break

    return products, environment, completed, is_risk_block


def run(settings: Settings) -> int:
    """执行一次完整抓取任务，返回进程退出码。"""
    setup_logging()
    settings.ensure_dirs()

    print("=" * 90)
    print("淘宝 MTop HTTP 多关键词抓取器")
    print("=" * 90)

    session: requests.Session | None = None
    environment: dict = {}
    all_results: dict[str, list[dict]] = {}
    incomplete_keywords: list[str] = []
    output_path: Path | None = None
    exit_code = 0

    try:
        session, environment = auth.prepare_session(settings)
        logging.info(f"UA: {environment['userAgent']}")
        logging.info(
            f"Screen={environment['screenResolution']} "
            f"View={environment['viewResolution']}"
        )

        keywords = [str(k).strip() for k in settings.keywords if str(k).strip()]
        if not keywords:
            raise RuntimeError("没有有效搜索词")

        # 上次运行若以风控收尾，先补足冷却，避免失败后立刻重跑继续打
        cooling.enforce_cross_process_cooldown(keywords, settings)

        for keyword in keywords:
            print("\n" + "=" * 90)
            print(f"开始关键词: {keyword}")
            print("=" * 90)

            try:
                products, environment, completed, is_risk_block = (
                    _scrape_with_risk_circuit(
                        session=session,
                        environment=environment,
                        keyword=keyword,
                        settings=settings,
                    )
                )
                all_results[keyword] = products
                if not completed:
                    _mark_incomplete(incomplete_keywords, keyword)
            except Exception as e:
                logging.exception(f"[{keyword}] 抓取异常，将尝试导出已保存断点数据: {e}")
                cp = load_checkpoint(keyword, settings)
                all_results[keyword] = visible_products(cp, settings)
                _mark_incomplete(incomplete_keywords, keyword)

        if not all_results:
            raise RuntimeError("没有有效搜索词或没有抓到数据")

        exit_code = 2 if incomplete_keywords else 0

    finally:
        # 无论正常结束、Ctrl+C 还是异常，都尽量把已抓到的数据落盘
        if session is not None and environment:
            try:
                auth.save_auth_state(session, environment, settings)
            except Exception as e:
                logging.warning(f"保存登录状态失败: {e}")

        if all_results and environment:
            try:
                output_path = export_excel(all_results, environment, settings)
            except Exception as e:
                logging.exception(f"Excel 导出失败: {e}")
                exit_code = 1

    if output_path is not None:
        print("\n" + "=" * 90)
        print("任务完成" if exit_code == 0 else "任务部分完成")
        print("=" * 90)
        for keyword, rows in all_results.items():
            videos = sum(1 for x in rows if x.get("has_video"))
            ads = sum(1 for x in rows if x.get("is_ad"))
            print(f"{keyword}: {len(rows)} 条 | 视频 {videos} | 广告 {ads}")
        print(f"Excel: {output_path}")
        print(f"登录状态: {settings.auth_state_file}")
        print(f"断点目录: {settings.checkpoint_dir}")
        print(f"商品主图: {'已嵌入' if settings.embed_images else '未嵌入'}")
        print(f"视频信息: {'已导出' if settings.export_video else '未导出'}")
        if incomplete_keywords:
            print("未完成关键词: " + " | ".join(incomplete_keywords))
            print("原因通常是淘宝访问验证。已成功页仍已导出；下次运行会从失败页继续。")

    return exit_code
