from __future__ import annotations

from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reddit_scraper.exceptions import HardStopHTTP, RequestBudgetReached, VerificationRequired
from reddit_scraper.http.client import RedditHTTPClient
from reddit_scraper.models import Loader, MediaItem
from reddit_scraper.parser.media import extract_media, kind_from_url
from reddit_scraper.parser.shreddit import parse_comments, parse_post
from reddit_scraper.services.media import MediaDownloader
from reddit_scraper.storage.checkpoint import CheckpointStore, ResumeState, loader_key
from reddit_scraper.storage.excel import write_post_workbook


def _add_loaders(state: ResumeState, loaders: list[Loader]):
    pending_keys = {loader_key(x) for x in list(state.pending_top) + list(state.pending_reply)}
    for loader in loaders:
        key = loader_key(loader)
        if key in state.processed_loader_keys or key in pending_keys:
            continue
        (state.pending_top if loader.top_level else state.pending_reply).append(loader)
        pending_keys.add(key)


def _merge_comments(existing: dict[str, dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    new_rows: list[dict[str, Any]] = []
    for row in rows:
        cid = str(row.get("comment_id") or "")
        if not cid:
            continue
        if cid not in existing:
            new_rows.append(row)
        existing[cid] = row
    return new_rows


class ScrapeRunner:
    def __init__(
        self,
        client: RedditHTTPClient,
        checkpoint_dir: Path,
        session_dir: Path,
        media_config: dict[str, Any] | None = None,
    ):
        self.client = client
        self.checkpoint_dir = checkpoint_dir
        self.session_dir = session_dir
        self.media_config = media_config or {}
        self.media_rows: list[dict[str, Any]] = []

    def _downloader(self, post_id: str) -> MediaDownloader:
        dir_name = str(self.media_config.get("dir_name") or "media")
        return MediaDownloader(self.client, self.session_dir / dir_name / post_id, self.media_config)

    def _download_post_media(self, bare: str, html_text: str, referer: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """解析并下载帖子里的图片/视频。失败不影响评论抓取，只记录状态。"""
        try:
            items = extract_media(html_text)
        except Exception as exc:
            print(f"[MEDIA] 解析失败，跳过: {type(exc).__name__}: {exc}")
            return [], {"media_type": "none", "media_count": 0, "media_dir": ""}
        if not items:
            print("[MEDIA] 未检测到图片/视频")
            return [], {"media_type": "none", "media_count": 0, "media_dir": ""}

        kinds = Counter(str(x.kind) for x in items)
        print(f"[MEDIA] 检测到 {len(items)} 个媒体项 {dict(kinds)}")
        try:
            rows, summary = self._downloader(bare).download_post(bare, items, referer=referer)
        except Exception as exc:
            print(f"[MEDIA] 下载异常，跳过: {type(exc).__name__}: {exc}")
            return [], {"media_type": "none", "media_count": 0, "media_dir": ""}
        ok = sum(1 for r in rows if r.get("status") == "成功")
        print(f"[MEDIA] 下载完成 {ok}/{len(rows)} -> {summary.get('media_dir') or '（无）'}")
        return rows, summary

    def _download_comment_media(
        self,
        bare: str,
        comment_rows: list[dict[str, Any]],
        referer: str,
    ) -> list[dict[str, Any]]:
        """Download comment-owned media only.

        MediaDownloader.download_comment_media() returns a LIST of records.
        Older scraper code incorrectly tried to unpack it as (records, summary),
        causing: ValueError: not enough values to unpack.
        """
        if not self.media_config.get("download_comment_media"):
            return []

        targets: list[tuple[dict[str, Any], str]] = []
        seen: set[tuple[str, str]] = set()

        for row in comment_rows:
            comment_id = str(row.get("comment_id") or "").strip()
            raw_urls = str(row.get("media_urls") or "")

            for raw in raw_urls.split("|"):
                url = raw.strip()
                if not url:
                    continue

                # Same comment + same Reddit media object = one download task.
                key = (comment_id, url.split("?", 1)[0])
                if key in seen:
                    continue
                seen.add(key)
                targets.append((row, url))

        if not targets:
            print("[MEDIA] 评论媒体 0 个")
            return []

        print(f"[MEDIA] 评论媒体 {len(targets)} 个，开始下载")

        downloader = self._downloader(bare)
        rows: list[dict[str, Any]] = []

        for row, url in targets:
            comment_id = str(row.get("comment_id") or "c").strip() or "c"

            item = MediaItem(
                kind=kind_from_url(url),
                url=url,
                source="comment.media_urls",
            )

            result = downloader.download_comment_media(
                bare,
                comment_id,
                [item],
                referer=referer,
            )

            # Current MediaDownloader returns list[dict].
            # Keep tuple compatibility in case an older local module is restored later.
            if isinstance(result, tuple):
                records = result[0]
            else:
                records = result

            if not isinstance(records, list):
                raise TypeError(
                    "MediaDownloader.download_comment_media() "
                    f"returned unexpected type: {type(records).__name__}"
                )

            for record in records:
                if not isinstance(record, dict):
                    continue
                # Enforce ownership at the final boundary so Excel can never
                # confuse a comment image with an original-post image.
                record["post_id"] = bare
                record["comment_id"] = comment_id
                record["media_scope"] = "comment"
                rows.append(record)

        ok = sum(
            1 for r in rows
            if r.get("status") in {"成功", "已存在", "链接"}
        )
        print(f"[MEDIA] 评论媒体下载完成 {ok}/{len(rows)}")
        return rows

    def scrape_post(self, post_url: str, sort: str, max_comments: int) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
        print(f"\n[POST] {post_url}")
        r1 = self.client.fetch_post(post_url)
        if r1.status_code != 200:
            raise RuntimeError(f"Post HTTP returned {r1.status_code}")
        post = parse_post(r1.text)
        if not post:
            raise RuntimeError("Could not parse <shreddit-post> from Reddit response")
        if not post.get("logged_in_marker"):
            print("[WARN] Reddit post response did not expose the logged-in marker")

        bare = str(post["post_id"]).removeprefix("t3_")
        subreddit = str(post["subreddit"])
        self.media_rows, media_summary = self._download_post_media(bare, r1.text, post_url)
        store = CheckpointStore(self.checkpoint_dir, bare, sort)
        comments = store.load_comments()
        state = store.load_state()
        print(f"[RESUME] existing comments={len(comments)} pending={len(state.pending_top)+len(state.pending_reply)} completed={state.completed}")

        # Always refresh the sorted comment forest. This repairs stale/empty queue state and refreshes visible scores.
        r2 = self.client.fetch_comment_forest(post_url, subreddit, bare, sort)
        if r2.status_code != 200:
            raise RuntimeError(f"Comment forest HTTP returned {r2.status_code}")
        parsed = parse_comments(r2.text)
        new_rows = _merge_comments(comments, parsed.comments)
        store.append_comments(new_rows)
        _add_loaders(state, parsed.loaders)
        state.completed = False
        store.save_state(state)
        print(
            f"[SEED] comments={len(parsed.comments)} new={len(new_rows)} total={len(comments)} "
            f"loaders={len(parsed.loaders)} top={sum(1 for x in parsed.loaders if x.top_level)} "
            f"rate_remaining={r2.rate_remaining}"
        )

        # 帖子媒体在抓评论前已下载，写入 media_summary / self.media_rows（见 _download_post_media 调用处）
        def export_current() -> tuple[dict[str, Any], list[dict[str, Any]], Path]:
            # 行顺序交给导出层的评论树构建（build_comment_tree）处理，这里只输出原始集合
            rows_now = list(comments.values())
            post_now = dict(post)
            post_now.update({
                "collected_comments": len(rows_now),
                "full_url": post_url,
                "scrape_sort": sort,
                "scrape_time": datetime.now(timezone.utc).isoformat(),
                "media_type": media_summary.get("media_type", ""),
                "media_count": media_summary.get("media_count", 0),
                "media_dir": media_summary.get("media_dir", ""),
            })
            out_now = self.session_dir / f"{bare}_{sort}.xlsx"
            write_post_workbook(out_now, post_now, rows_now, self.media_rows)
            return post_now, rows_now, out_now

        # 评论抓取结束后才调用：可选下载评论里的贴图
        def on_comments_collected():
            if not self.media_config.get("download_comment_media"):
                return
            try:
                extra = self._download_comment_media(bare, list(comments.values()), post_url)
                if extra:
                    self.media_rows.extend(extra)
            except Exception as exc:
                print(f"[MEDIA] 评论媒体下载异常，跳过: {type(exc).__name__}: {exc}")

        request_no = 0
        try:
            while state.pending_top or state.pending_reply:
                if max_comments > 0 and len(comments) >= max_comments:
                    print(f"[LIMIT] reached max_comments={max_comments}; pending loaders saved for resume")
                    break

                # Prefer top-level pagination so the scraper does not get trapped deep in one reply branch.
                q: deque[Loader] = state.pending_top if state.pending_top else state.pending_reply
                loader = q.popleft()
                key = loader_key(loader)
                kind = "TOP" if loader.top_level else "REPLY"
                try:
                    request_no += 1
                    print(
                        f"[MORE {request_no}] {kind} total={len(comments)} pending_top={len(state.pending_top)} "
                        f"pending_reply={len(state.pending_reply)}"
                    )
                    resp = self.client.fetch_loader(post_url, loader, sort)
                except (HardStopHTTP, VerificationRequired, RequestBudgetReached):
                    q.appendleft(loader)
                    store.save_state(state)
                    raise
                except Exception:
                    q.appendleft(loader)
                    store.save_state(state)
                    raise

                if resp.status_code in (400, 404, 410):
                    state.processed_loader_keys.add(key)
                    print(f"[STALE] loader returned HTTP {resp.status_code}; skipped")
                    store.save_state(state)
                    continue
                if resp.status_code != 200:
                    q.appendleft(loader)
                    store.save_state(state)
                    raise RuntimeError(f"more-comments HTTP returned {resp.status_code}")

                page = parse_comments(resp.text)
                added = _merge_comments(comments, page.comments)
                store.append_comments(added)
                state.processed_loader_keys.add(key)
                _add_loaders(state, page.loaders)
                store.save_state(state)
                print(
                    f"[OK] got={len(page.comments)} new={len(added)} total={len(comments)} "
                    f"next={len(page.loaders)} rate_remaining={resp.rate_remaining}"
                )
        except Exception:
            post_partial, rows_partial, output_partial = export_current()
            print(f"[PARTIAL EXCEL] {output_partial} ({len(rows_partial)} comments)")
            raise

        if not state.pending_top and not state.pending_reply:
            state.completed = True
            store.save_state(state)
            print("[DONE] Reddit returned no more comment loaders")

        on_comments_collected()
        post_out, rows, output = export_current()
        return post_out, rows, str(output)