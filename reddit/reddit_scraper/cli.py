from __future__ import annotations

from pathlib import Path
from urllib.parse import quote_plus

from reddit_scraper.config import load_config
from reddit_scraper.exceptions import HardStopHTTP, RequestBudgetReached, VerificationRequired
from reddit_scraper.http.client import RedditHTTPClient
from reddit_scraper.safety.guard import GuardConfig, RequestGuard
from reddit_scraper.services.bootstrap import bootstrap_from_browser
from reddit_scraper.services.scraper import ScrapeRunner
from reddit_scraper.services.searcher import KeywordSearchRunner
from reddit_scraper.storage.excel import write_combined_search_workbook, write_combined_workbook
from reddit_scraper.utils.text import parse_input_urls, parse_keywords, stamp


def _prompt_int(label: str, default: int) -> int:
    raw = input(f"{label} [{default}]: ").strip()
    if not raw:
        return default
    return max(0, int(raw))


def _make_guard(cfg: dict) -> RequestGuard:
    safety_cfg = cfg["safety"]
    return RequestGuard(GuardConfig(
        max_requests=int(safety_cfg["max_http_requests_per_run"]),
        hard_stop_status_codes=tuple(int(x) for x in safety_cfg["hard_stop_status_codes"]),
        rate_remaining_pause_threshold=float(safety_cfg["rate_remaining_pause_threshold"]),
        rate_reset_padding_seconds=float(safety_cfg["rate_reset_padding_seconds"]),
        stop_on_verification=bool(safety_cfg["stop_on_verification"]),
    ))


def _bootstrap(cfg: dict, url: str):
    print("\n[BOOTSTRAP] opening one temporary Reddit tab in the logged-in AdsPower profile...")
    bootstrap = bootstrap_from_browser(url, timeout_seconds=int(cfg["bootstrap"]["timeout_seconds"]))
    print(
        f"[OK] Browser={bootstrap.browser.name} pid={bootstrap.browser.pid} "
        f"Chrome={bootstrap.browser.browser} cookies={len(bootstrap.cookies)} csrf=present"
    )
    print("[SECURITY] Cookies and CSRF values remain in memory and are not written to output files.")
    return bootstrap


def _run_post_mode(root: Path, cfg: dict) -> int:
    raw = input("Reddit post URL(s), separated by spaces/commas, or TXT path [input\\urls.txt]: ").strip()
    urls = parse_input_urls(raw, root)
    if not urls:
        print("No Reddit post URLs found.")
        return 2

    scrape_cfg = cfg["scrape"]
    max_comments = _prompt_int("Max comments per post (0 = all available)", int(scrape_cfg["default_max_comments_per_post"]))
    default_sort = str(scrape_cfg["default_sort"])
    sort = input(f"Comment sort [{default_sort}] ({'/'.join(scrape_cfg['allowed_sorts'])}): ").strip().lower() or default_sort
    if sort not in scrape_cfg["allowed_sorts"]:
        raise ValueError(f"Unsupported sort: {sort}")

    guard = _make_guard(cfg)
    print("\n" + "-" * 100)
    print(f"Mode            : Post detail + comments")
    print(f"Posts           : {len(urls)}")
    print(f"Max comments    : {'ALL' if max_comments == 0 else max_comments} / post")
    print(f"Sort            : {sort}")
    print(f"HTTP run budget : {guard.cfg.max_requests}")
    print(f"Hard-stop HTTP  : {list(guard.cfg.hard_stop_status_codes)}")
    print(f"Min interval    : {cfg['http']['min_interval_seconds']}s")
    print("-" * 100)

    bootstrap = _bootstrap(cfg, urls[0])
    client = RedditHTTPClient(bootstrap, cfg["http"], guard)
    runtime_dir = root / cfg["output"]["runtime_dir"]
    checkpoint_dir = root / cfg["output"]["checkpoint_dir"]
    session_dir = runtime_dir / f"session_{stamp()}"
    session_dir.mkdir(parents=True, exist_ok=True)
    runner = ScrapeRunner(client, checkpoint_dir, session_dir, media_config=cfg.get("media"))

    all_posts = []
    all_comments = []
    all_media = []
    try:
        for idx, url in enumerate(urls, 1):
            print("\n" + "=" * 100)
            print(f"[{idx}/{len(urls)}]")
            post, comments, path = runner.scrape_post(url, sort, max_comments)
            all_posts.append(post)
            all_comments.extend(comments)
            all_media.extend(runner.media_rows)
            print(f"[EXCEL] {path}")
    except RequestBudgetReached as exc:
        print(f"\n[STOP] {exc}")
        print("Checkpoints were preserved. Rerun to continue.")
    except HardStopHTTP as exc:
        print(f"\n[STOP] Reddit returned hard-stop HTTP {exc.status_code}.")
        print("Do not force retries. Keep the AdsPower session stable and rerun later.")
    except VerificationRequired as exc:
        print(f"\n[STOP] {exc}")
        print("Complete Reddit verification manually in AdsPower, then rerun to resume.")

    if all_posts:
        combined = session_dir / "reddit_posts_comments_combined.xlsx"
        write_combined_workbook(combined, all_posts, all_comments, all_media)
        print("\n" + "=" * 100)
        print(f"HTTP requests   : {guard.requests_made}")
        print(f"Posts exported  : {len(all_posts)}")
        print(f"Comments        : {len(all_comments)}")
        print(f"Media items     : {len(all_media)}")
        print(f"Combined Excel  : {combined}")
        print(f"Session folder  : {session_dir}")
        print("=" * 100)
    return 0


def _run_search_mode(root: Path, cfg: dict) -> int:
    raw = input("Keyword(s), separated by | or comma, or TXT path [input\\keywords.txt]: ").strip()
    keywords = parse_keywords(raw, root)
    if not keywords:
        print("No keywords found.")
        return 2

    scfg = cfg["search"]
    max_posts = _prompt_int("Max posts per keyword (0 = all listing pages Reddit exposes)", int(scfg["default_max_posts_per_keyword"]))
    default_sort = str(scfg["default_sort"])
    sort = input(f"Search sort [{default_sort}] ({'/'.join(scfg['allowed_sorts'])}): ").strip().lower() or default_sort
    if sort not in scfg["allowed_sorts"]:
        raise ValueError(f"Unsupported search sort: {sort}")
    default_time = str(scfg["default_time"])
    time_filter = input(f"Time filter [{default_time}] ({'/'.join(scfg['allowed_times'])}): ").strip().lower() or default_time
    if time_filter not in scfg["allowed_times"]:
        raise ValueError(f"Unsupported search time: {time_filter}")
    subreddit = input("Subreddit filter [blank = all Reddit, example: officechairs]: ").strip()
    subreddit = subreddit.removeprefix("r/").strip(" /")

    guard = _make_guard(cfg)
    print("\n" + "-" * 100)
    print("Mode            : Keyword search -> post list")
    print(f"Keywords        : {len(keywords)}")
    print(f"Max posts       : {'ALL' if max_posts == 0 else max_posts} / keyword")
    print(f"Sort            : {sort}")
    print(f"Time            : {time_filter}")
    print(f"Subreddit       : {subreddit or 'ALL REDDIT'}")
    print(f"HTTP run budget : {guard.cfg.max_requests}")
    print(f"Hard-stop HTTP  : {list(guard.cfg.hard_stop_status_codes)}")
    print(f"Min interval    : {cfg['http']['min_interval_seconds']}s")
    print("-" * 100)

    bootstrap_url = f"https://www.reddit.com/search/?q={quote_plus(keywords[0])}&type=posts"
    bootstrap = _bootstrap(cfg, bootstrap_url)
    client = RedditHTTPClient(bootstrap, cfg["http"], guard)
    runtime_dir = root / cfg["output"]["runtime_dir"]
    checkpoint_dir = root / cfg["output"]["checkpoint_dir"]
    session_dir = runtime_dir / f"search_session_{stamp()}"
    session_dir.mkdir(parents=True, exist_ok=True)
    runner = KeywordSearchRunner(client, checkpoint_dir, session_dir)

    all_rows = []
    try:
        for idx, keyword in enumerate(keywords, 1):
            print("\n" + "=" * 100)
            print(f"[{idx}/{len(keywords)}] {keyword}")
            rows, path = runner.search_keyword(
                keyword,
                max_posts=max_posts,
                sort=sort,
                time_filter=time_filter,
                subreddit=subreddit,
            )
            all_rows.extend(rows)
            print(f"[EXCEL] {path}")
    except RequestBudgetReached as exc:
        print(f"\n[STOP] {exc}")
        print("Search checkpoints were preserved. Rerun to continue.")
    except HardStopHTTP as exc:
        print(f"\n[STOP] Reddit returned hard-stop HTTP {exc.status_code}.")
        print("Do not force retries. Keep the AdsPower session stable and rerun later.")
    except VerificationRequired as exc:
        print(f"\n[STOP] {exc}")
        print("Complete Reddit verification manually in AdsPower, then rerun to resume.")

    if all_rows:
        combined = session_dir / "reddit_search_posts_combined.xlsx"
        write_combined_search_workbook(combined, all_rows)
        print("\n" + "=" * 100)
        print(f"HTTP requests   : {guard.requests_made}")
        print(f"Keywords        : {len(keywords)}")
        print(f"Rows exported   : {len(all_rows)}")
        print(f"Combined Excel  : {combined}")
        print(f"Session folder  : {session_dir}")
        print("=" * 100)
    return 0


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config" / "settings.json")

    print("=" * 100)
    print("Reddit HTTP Scraper V1.2 - Post Comments + Keyword Search")
    print("=" * 100)
    print("Uses the currently logged-in Reddit session in AdsPower/SunBrowser.")
    print("No CAPTCHA solving/bypass. 403/429/503 or verification stops and preserves checkpoints.\n")
    print("1) Post detail + full comment tree")
    print("2) Keyword search -> post list")
    mode = input("Mode [1]: ").strip() or "1"
    if mode in {"1", "post", "posts", "detail"}:
        return _run_post_mode(root, cfg)
    if mode in {"2", "search", "keyword", "keywords"}:
        return _run_search_mode(root, cfg)
    raise ValueError(f"Unsupported mode: {mode}")


if __name__ == "__main__":
    raise SystemExit(main())
