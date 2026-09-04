from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reddit_scraper.http.client import RedditHTTPClient
from reddit_scraper.parser.search import parse_search_listing
from reddit_scraper.storage.excel import write_search_workbook


class SearchCheckpoint:
    def __init__(self, root: Path, key: str):
        root.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)[:160]
        self.rows_path = root / f"search_{safe}.jsonl"
        self.state_path = root / f"search_{safe}.state.json"

    def load_rows(self) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        if not self.rows_path.exists():
            return rows
        for line in self.rows_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(line)
            except Exception:
                continue
            pid = str(obj.get("post_id") or obj.get("bare_post_id") or "")
            if pid:
                rows[pid] = obj
        return rows

    def append_rows(self, rows: list[dict[str, Any]]):
        if not rows:
            return
        with self.rows_path.open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"after": None, "completed": False, "pages": 0}
        try:
            obj = json.loads(self.state_path.read_text(encoding="utf-8"))
            return {
                "after": obj.get("after"),
                "completed": bool(obj.get("completed")),
                "pages": int(obj.get("pages") or 0),
            }
        except Exception:
            return {"after": None, "completed": False, "pages": 0}

    def save_state(self, state: dict[str, Any]):
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)


class KeywordSearchRunner:
    def __init__(self, client: RedditHTTPClient, checkpoint_dir: Path, session_dir: Path):
        self.client = client
        self.checkpoint_dir = checkpoint_dir
        self.session_dir = session_dir

    def search_keyword(
        self,
        keyword: str,
        *,
        max_posts: int,
        sort: str,
        time_filter: str,
        subreddit: str = "",
    ) -> tuple[list[dict[str, Any]], str]:
        checkpoint_key = f"{keyword}_{subreddit or 'all'}_{sort}_{time_filter}"
        store = SearchCheckpoint(self.checkpoint_dir, checkpoint_key)
        existing = store.load_rows()
        state = store.load_state()
        after = state.get("after")
        pages = int(state.get("pages") or 0)

        print(
            f"[SEARCH] keyword={keyword!r} subreddit={subreddit or 'ALL'} sort={sort} "
            f"time={time_filter} existing={len(existing)} after={'Y' if after else 'N'}"
        )

        while True:
            if max_posts > 0 and len(existing) >= max_posts:
                print(f"[LIMIT] reached max_posts={max_posts}")
                break
            if state.get("completed"):
                print("[RESUME] checkpoint already completed")
                break

            limit = 100
            if max_posts > 0:
                limit = max(1, min(100, max_posts - len(existing)))

            result = self.client.search_posts(
                keyword=keyword,
                sort=sort,
                time_filter=time_filter,
                subreddit=subreddit,
                limit=limit,
                after=(str(after) if after else None),
                count=len(existing),
            )
            if result.status_code != 200:
                raise RuntimeError(f"Reddit search HTTP returned {result.status_code}")
            ctype = (result.content_type or "").lower()
            if "json" not in ctype:
                sample = (result.text or "")[:120].replace("\n", " ")
                raise RuntimeError(f"Reddit search did not return JSON ({result.content_type}): {sample}")

            rows, next_after = parse_search_listing(
                result.text,
                keyword=keyword,
                sort=sort,
                time_filter=time_filter,
                subreddit_filter=subreddit,
                start_rank=len(existing) + 1,
            )
            new_rows: list[dict[str, Any]] = []
            for row in rows:
                pid = str(row.get("post_id") or row.get("bare_post_id") or "")
                if pid and pid not in existing:
                    row["search_collected_at"] = datetime.now(timezone.utc).isoformat()
                    existing[pid] = row
                    new_rows.append(row)
            store.append_rows(new_rows)

            pages += 1
            after = next_after
            state = {"after": after, "completed": not bool(after), "pages": pages}
            store.save_state(state)
            print(
                f"[PAGE {pages}] got={len(rows)} new={len(new_rows)} total={len(existing)} "
                f"after={'Y' if after else 'N'} rate_remaining={result.rate_remaining}"
            )

            if not after:
                print("[DONE] Reddit search listing returned no after cursor")
                break
            if not rows:
                print("[DONE] empty search page")
                break

        output_rows = list(existing.values())
        output_rows.sort(key=lambda r: int(r.get("result_rank") or 0))
        safe_kw = "".join(c if c.isalnum() or c in "-_" else "_" for c in keyword)[:70] or "keyword"
        safe_sr = ("_r_" + subreddit) if subreddit else ""
        out = self.session_dir / f"search_{safe_kw}{safe_sr}_{sort}_{time_filter}.xlsx"
        write_search_workbook(out, output_rows)
        return output_rows, str(out)
