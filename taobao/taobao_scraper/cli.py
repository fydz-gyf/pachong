"""命令行与交互式参数配置。"""

from __future__ import annotations

import argparse
import re
import shlex

from .config import Settings


def parse_keywords_text(raw: str) -> list[str]:
    """
    关键词输入规则：

    1. 推荐多个关键词用逗号/中文逗号/分号/竖线分隔：
         coffee table, accent chair, end table
       这样英文短语本身可以直接带空格。

    2. 带引号时按 shell 规则解析：
         "coffee table" "accent chair" 沙发椅

    3. 没有显式分隔符时：
       - 含英文字符：整行视为 1 个关键词，例如 coffee table
       - 纯中文/数字词：仍允许空格分成多个，例如 沙发 餐椅 茶几

    这样可以避免把 coffee table 错拆成 coffee 和 table。
    """
    raw = str(raw or "").strip()
    if not raw:
        return []

    if re.search(r"[|,，;；]", raw):
        parts = re.split(r"[|,，;；]+", raw)
        return [x.strip().strip('"').strip("'") for x in parts if x.strip()]

    if '"' in raw or "'" in raw:
        try:
            parts = shlex.split(raw, posix=True)
        except ValueError:
            parts = raw.split()
        return [x.strip() for x in parts if x.strip()]

    if re.search(r"[A-Za-z]", raw):
        return [raw]

    return [x.strip() for x in raw.split() if x.strip()]


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        value = input(f"{prompt} {suffix}: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes", "1", "是", "要"}:
            return True
        if value in {"n", "no", "0", "否", "不要"}:
            return False
        print("请输入 Y 或 N。")


def interactive_configure(settings: Settings) -> Settings:
    print("\n" + "=" * 90)
    print("淘宝商品抓取参数")
    print("=" * 90)
    print("英文短语可直接输入，例如：coffee table")
    print("多个关键词推荐用逗号分隔，例如：coffee table, accent chair, end table")
    print("纯中文多个关键词也可空格分隔，例如：沙发 餐椅 茶几")
    print('也支持引号写法："accent chair" "coffee table" 沙发椅')

    while True:
        raw = input("\n请输入关键词: ").strip()
        keywords = parse_keywords_text(raw)
        if keywords:
            settings.keywords = keywords
            break
        print("至少输入 1 个关键词。")

    while True:
        raw_pages = input("每个关键词抓取多少页（1-100）: ").strip()
        try:
            pages = int(raw_pages)
            if 1 <= pages <= 100:
                settings.max_pages = pages
                break
        except Exception:
            pass
        print("请输入 1-100 的整数。")

    settings.embed_images = ask_yes_no("是否把商品主图嵌入 Excel", default=True)

    if settings.embed_images:
        print("图片下载并发数越高越快，但过高容易触发阿里图片 CDN 的 SSL/TLS 断连。")
        print("建议：3-6；网络稳定可尝试 8；不建议超过 12。")
        while True:
            raw_workers = input(
                f"图片下载并发数（1-32，直接回车默认 {settings.image_workers}）: "
            ).strip()
            if not raw_workers:
                break
            try:
                workers = int(raw_workers)
                if 1 <= workers <= 32:
                    settings.image_workers = workers
                    break
            except Exception:
                pass
            print("请输入 1-32 的整数。")

    settings.export_video = ask_yes_no(
        "是否导出视频信息（视频链接/视频封面，不下载视频文件）", default=True
    )

    print("\n本次任务：")
    print("  关键词：" + " | ".join(settings.keywords))
    print(f"  页数：每个关键词最多 {settings.max_pages} 页")
    print(
        f"  商品主图：{'嵌入 Excel' if settings.embed_images else '不嵌入（仍保留主图URL）'}"
    )
    if settings.embed_images:
        print(f"  图片下载并发：{settings.image_workers}")
    print(f"  视频信息：{'导出' if settings.export_video else '不导出'}")
    print("=" * 90 + "\n")
    return settings


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="taobao_scraper",
        description="淘宝 MTop HTTP 多关键词抓取器",
    )
    parser.add_argument(
        "--interactive", action="store_true", help="启动交互式参数输入"
    )
    parser.add_argument(
        "--keywords", nargs="+", help="关键词列表，英文短语请加引号"
    )
    parser.add_argument("--pages", type=int, help="每个关键词抓取页数，1-100")
    parser.add_argument("--no-images", action="store_true", help="不把主图嵌入Excel")
    parser.add_argument("--image-workers", type=int, help="图片下载并发数，1-32")
    parser.add_argument(
        "--image-timeout",
        type=float,
        help="单张主图的下载时间预算（秒），默认 40",
    )
    parser.add_argument("--no-video", action="store_true", help="不导出视频字段")
    parser.add_argument(
        "--timestamp",
        action="store_true",
        help="Excel 文件名追加时间戳，避免重跑覆盖上次结果",
    )
    parser.add_argument("--no-resume", action="store_true", help="忽略断点，从头抓取")
    parser.add_argument(
        "--force-refresh", action="store_true", help="无视旧断点，从第 1 页重新抓"
    )
    parser.add_argument(
        "--save-raw", action="store_true", help="保存每页完整原始 API JSON"
    )
    parser.add_argument("--filter-ads", action="store_true", help="过滤广告商品")
    parser.add_argument(
        "--no-cross-cooldown",
        action="store_true",
        help="关闭跨进程风控冷却（上次风控后立刻重跑）",
    )
    parser.add_argument("--cdp-port", type=int, help="Chrome CDP 端口，默认 9222")
    return parser


def settings_from_cli(settings: Settings, argv: list[str] | None = None) -> Settings:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.interactive:
        return interactive_configure(settings)

    if args.keywords:
        settings.keywords = [str(x).strip() for x in args.keywords if str(x).strip()]
    if args.pages is not None:
        if not 1 <= args.pages <= 100:
            parser.error("--pages 必须为 1-100")
        settings.max_pages = args.pages
    if args.no_images:
        settings.embed_images = False
    if args.image_workers is not None:
        if not 1 <= args.image_workers <= 32:
            parser.error("--image-workers 必须为 1-32")
        settings.image_workers = args.image_workers
    if args.image_timeout is not None:
        if args.image_timeout <= 0:
            parser.error("--image-timeout 必须大于 0")
        settings.image_total_timeout = args.image_timeout
    if args.no_video:
        settings.export_video = False
    if args.timestamp:
        settings.excel_append_timestamp = True
    if args.no_resume:
        settings.resume = False
    if args.force_refresh:
        settings.force_refresh = True
    if args.save_raw:
        settings.save_raw_response = True
    if args.filter_ads:
        settings.filter_ads = True
    if args.no_cross_cooldown:
        settings.cross_process_cooldown_min = 0.0
    if args.cdp_port is not None:
        settings.cdp_port = args.cdp_port

    return settings
