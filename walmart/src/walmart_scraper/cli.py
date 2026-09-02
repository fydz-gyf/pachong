from __future__ import annotations

import argparse
import logging
import os
import re
import shlex
import sys
from pathlib import Path

from .config import Settings
from .exporters.excel import ExcelExporter
from .http.client import WalmartHttpClient
from .media.images import ImageDownloader
from .services.search import SearchService
from .storage.checkpoints import CheckpointStore


def parse_keyword_input(text: str) -> list[str]:
    text = str(text or "").strip()
    if not text:
        return []
    normalized = text.replace("，", ",").replace("；", ";")
    if "," in normalized or ";" in normalized:
        return [p.strip().strip('"\'') for p in re.split(r"[,;]+", normalized) if p.strip()]
    # Quoted phrases can be used without commas: "office chair" "coffee table"
    if '"' in text or "'" in text:
        try:
            parts = [x.strip() for x in shlex.split(text) if x.strip()]
            if parts:
                return parts
        except ValueError:
            pass
    # An unquoted English phrase such as office chair is one keyword.
    if re.search(r"[A-Za-z]", text):
        return [text]
    # Pure CJK input keeps the Taobao-script convention: spaces separate keywords.
    return [x for x in text.split() if x.strip()]



def _ask_int(prompt: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    raw = input(prompt).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def apply_interactive_defaults(args):
    """Taobao-style interactive startup when -k/--keywords was not supplied."""
    if args.keywords is not None or not getattr(sys.stdin, "isatty", lambda: False)():
        return args

    print("=" * 90)
    print("Walmart HTTP 多关键词商品抓取器")
    print("=" * 90)
    print("英文短语可直接输入，例如：office chair")
    print("多个关键词推荐用逗号分隔，例如：office chair, coffee table, accent chair")
    print('也支持引号写法："office chair" "coffee table"')
    print()

    while True:
        keywords = parse_keyword_input(input("请输入关键词: "))
        if keywords:
            break
        print("关键词不能为空。")

    args.keywords = keywords
    args.pages = _ask_int(f"每个关键词抓取多少页（1-100，默认 {args.pages}）: ", args.pages, 1, 100)

    image_answer = input("是否把商品主图嵌入 Excel [Y/n]: ").strip().lower()
    args.no_images = image_answer in {"n", "no", "0"}
    if not args.no_images:
        args.image_workers = _ask_int(
            f"图片下载并发数（1-20，直接回车默认 {args.image_workers}）: ",
            args.image_workers,
            1,
            20,
        )

    sorftime_answer = input("是否抓取 Sorftime 产品预计月销量/预计月销售额 [Y/n]: ").strip().lower()
    args.no_sorftime = sorftime_answer in {"n", "no", "0"}

    print()
    print("本次任务：")
    print("  关键词：" + " | ".join(args.keywords))
    print(f"  页数：每个关键词最多 {args.pages} 页")
    print("  商品主图：" + ("嵌入 Excel" if not args.no_images else "不嵌入"))
    if not args.no_images:
        print(f"  图片下载并发：{args.image_workers}")
    print(
        "  Sorftime数据："
        + (
            ("HTTP直连" if args.sorftime_mode == "http" else "浏览器DOM")
            + "抓取预计月销量/预计月销售额"
            if not args.no_sorftime
            else "不抓取"
        )
    )
    print("  浏览器身份：自动检测（优先复用已打开的 AdsPower/Walmart 配置）")
    print("=" * 90)
    print()
    return args

def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def build_parser():
    p = argparse.ArgumentParser(description="Modular Walmart HTTP product search scraper")
    p.add_argument("-k", "--keywords", nargs="*", help="search keywords")
    p.add_argument("-p", "--pages", type=int, default=5)
    p.add_argument("--proxy", default="")
    p.add_argument("--no-images", action="store_true")
    p.add_argument("--image-workers", type=int, default=5)
    p.add_argument("--no-sorftime", action="store_true",
                   help="disable Sorftime estimated monthly sales/revenue enrichment")
    p.add_argument("--sorftime-mode", choices=["http", "browser"], default="http",
                   help="Sorftime enrichment mode; V9 defaults to direct HTTP")
    p.add_argument("--no-sorftime-browser-fallback", action="store_true",
                   help="do not fall back to Sorftime's rendered browser cards when HTTP fails")
    p.add_argument("--sorftime-timeout", type=int, default=18,
                   help="browser fallback wait seconds when Sorftime HTTP is unavailable")
    p.add_argument("--filter-sponsored", action="store_true")
    p.add_argument("--force-refresh", action="store_true")
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--save-html", action="store_true")
    p.add_argument("--save-next-data", action="store_true")
    p.add_argument("--browser", choices=["auto", "chrome", "adspower"], default="auto",
                   help="browser source used for Walmart cookies/UA")
    p.add_argument("--cdp-port", type=int, default=9222,
                   help="Chrome CDP port when --browser chrome/auto")
    p.add_argument("--adspower-profile-id", default="",
                   help="AdsPower profile ID (preferred stable selector)")
    p.add_argument("--adspower-profile-no", default="",
                   help="AdsPower profile No. if you prefer the visible serial number")
    p.add_argument("--adspower-api-base", default="http://local.adspower.net:50325",
                   help="AdsPower Local API base URL")
    p.add_argument("--adspower-no-start", action="store_true",
                   help="do not start an inactive AdsPower profile automatically")
    p.add_argument("--no-adspower-proxy-sync", action="store_true",
                   help="do not copy the AdsPower profile proxy into the HTTP client")
    p.add_argument("--adspower-cdp-port", type=int, default=0,
                   help="attach directly to a running AdsPower Chromium CDP port; skips Local API")
    p.add_argument("--no-adspower-process-discovery", action="store_true",
                   help="disable Windows process-table fallback when AdsPower Local API is unavailable")
    p.add_argument("--no-browser-page-fallback", action="store_true",
                   help="disable visible-browser page loading when standalone HTTP is challenged")
    p.add_argument("--list-browser-cdp", action="store_true",
                   help="list detected local Chromium/AdsPower CDP endpoints and exit")
    return p


def main(argv=None):
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_parser().parse_args(argv)
    if args.list_browser_cdp:
        from .auth.process_discovery import discover_remote_debug_browsers
        rows = discover_remote_debug_browsers()
        if not rows:
            print("No live Chromium/AdsPower remote-debugging ports were detected.")
            return 1
        print("Detected browser CDP endpoints:")
        for row in rows:
            print(f"  {row.label} browser={row.browser or '-'}")
        return 0

    args = apply_interactive_defaults(args)

    settings = Settings(
        keywords=[x.strip() for x in (args.keywords or ["office chair"]) if x.strip()],
        max_pages=max(1, args.pages), proxy_url=args.proxy.strip(), embed_images=not args.no_images,
        image_workers=max(1, min(20, args.image_workers)), filter_sponsored=args.filter_sponsored,
        sorftime_enabled=not args.no_sorftime,
        sorftime_mode=args.sorftime_mode,
        sorftime_browser_fallback=not args.no_sorftime_browser_fallback,
        sorftime_wait_timeout=max(5, min(60, args.sorftime_timeout)),
        force_refresh=args.force_refresh, resume=not args.no_resume, save_html=args.save_html, save_next_data=args.save_next_data,
        browser_mode=args.browser, cdp_port=max(1, args.cdp_port),
        browser_page_fallback=not args.no_browser_page_fallback,
        adspower_profile_id=args.adspower_profile_id.strip(),
        adspower_profile_no=args.adspower_profile_no.strip(),
        adspower_api_base=args.adspower_api_base.strip(),
        adspower_auto_start=not args.adspower_no_start,
        adspower_sync_proxy=not args.no_adspower_proxy_sync,
        adspower_process_discovery=not args.no_adspower_process_discovery,
        adspower_cdp_port=max(0, args.adspower_cdp_port),
    ).bind_root(project_root())
    client = WalmartHttpClient(settings)
    service = SearchService(settings, client, CheckpointStore(settings))
    exporter = ExcelExporter(settings, ImageDownloader(settings))
    logging.info("HTTP client: %s", client.environment["client"])
    logging.info("Browser auth source: %s", client.browser.resolved_mode)
    if settings.browser_mode == "adspower" and not settings.proxy_url:
        logging.info("AdsPower profile has no reusable proxy configuration; HTTP will use the current machine network.")
    if settings.sorftime_enabled:
        logging.info(
            "Sorftime mode: %s%s",
            settings.sorftime_mode,
            " (browser fallback enabled)" if settings.sorftime_browser_fallback and settings.sorftime_mode == "http" else "",
        )
    all_results = {}
    incomplete = []
    for keyword in settings.keywords:
        try:
            rows, completed = service.scrape_keyword(keyword)
            all_results[keyword] = rows
            if not completed: incomplete.append(keyword)
        except Exception as e:
            logging.exception("[%s] failed: %s", keyword, e)
            cp = CheckpointStore(settings).load(keyword)
            rows = [x for x in (cp.get("products") or []) if int(x.get("page") or 0) <= settings.max_pages]
            for i, row in enumerate(rows, start=1): row["global_rank"] = i
            all_results[keyword] = rows; incomplete.append(keyword)
    output = exporter.export(all_results, client.environment)
    print(f"\n完成: {output}")
    if incomplete:
        print("未完整抓取: " + " | ".join(incomplete))
    if settings.sorftime_enabled:
        sorftime_incomplete = []
        for keyword, rows in all_results.items():
            total = len(rows)
            matched = sum(1 for r in rows if r.get("sorftime_checked"))
            estimates = sum(
                1 for r in rows
                if r.get("sorftime_month_sales") not in ("", None)
                or r.get("sorftime_month_revenue") not in ("", None)
            )
            logging.info(
                "[%s] Sorftime final coverage: matched=%s/%s estimates available=%s",
                keyword,
                matched,
                total,
                estimates,
            )
            if matched < total:
                sorftime_incomplete.append(keyword)
        if sorftime_incomplete:
            print("Sorftime数据未完整: " + " | ".join(sorftime_incomplete))
    return 0
