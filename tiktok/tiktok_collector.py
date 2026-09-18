# -*- coding: utf-8 -*-

import argparse
import json
import os
import queue
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qs, parse_qsl, urlencode

import requests
import websocket


# Keep all defaults relative to this copied project so the collector works in
# a clean checkout regardless of where the repository is cloned.
ROOT = Path(__file__).resolve().parent
RUN_ROOT = ROOT / "data"

SEARCH_API = "/api/search/general/full/"
COMMENT_API = "/api/comment/list/"
REPLY_API = "/api/comment/list/reply/"


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_json(path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def save_json(path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    tmp.replace(path)


def append_jsonl(path, obj):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def qparam(url, key, default=""):
    try:
        return parse_qs(
            urlparse(url).query
        ).get(key, [default])[0]
    except Exception:
        return default


def ads_headers(api_key):
    if api_key:
        return {
            "Authorization": f"Bearer {api_key}"
        }
    return {}


def get_active_browsers(api_key):
    last = None

    for base in [
        "http://local.adspower.net:50325",
        "http://127.0.0.1:50325",
        "http://localhost:50325",
    ]:
        try:
            r = requests.get(
                base + "/api/v1/browser/local-active",
                headers=ads_headers(api_key),
                timeout=5
            )

            data = r.json()

            if data.get("code") == 0:
                return data.get("data", {}).get("list", [])

            last = data

        except Exception as e:
            last = repr(e)

    raise RuntimeError(
        f"无法读取 AdsPower 当前环境: {last}"
    )


def find_tiktok_target(debug_port):
    targets = requests.get(
        f"http://127.0.0.1:{debug_port}/json/list",
        timeout=5
    ).json()

    # 优先已经打开的 TikTok 页面
    for t in targets:
        if (
            t.get("type") == "page"
            and "tiktok.com" in t.get("url", "").lower()
            and t.get("webSocketDebuggerUrl")
        ):
            return t

    # 没有 TikTok 页面则找普通 page
    for t in targets:
        if (
            t.get("type") == "page"
            and t.get("webSocketDebuggerUrl")
        ):
            return t

    raise RuntimeError(
        "AdsPower 中没有可以连接的页面。请先打开 TikTok。"
    )


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(
            ws_url,
            timeout=1,
            suppress_origin=True,
        )

        self.events = queue.Queue()
        self.pending = {}
        self.lock = threading.Lock()
        self.seq = 100
        self.closed = False

        self.thread = threading.Thread(
            target=self._recv_loop,
            daemon=True
        )
        self.thread.start()

    def _recv_loop(self):
        while not self.closed:
            try:
                raw = self.ws.recv()

                if not raw:
                    continue

                msg = json.loads(raw)

                if "id" in msg:
                    with self.lock:
                        q = self.pending.pop(
                            msg["id"],
                            None
                        )

                    if q:
                        q.put(msg)

                elif "method" in msg:
                    self.events.put(msg)

            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break

    def send(self, method, params=None, timeout=15):
        with self.lock:
            self.seq += 1
            cid = self.seq
            q = queue.Queue(maxsize=1)
            self.pending[cid] = q

        self.ws.send(json.dumps({
            "id": cid,
            "method": method,
            "params": params or {}
        }))

        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            with self.lock:
                self.pending.pop(cid, None)

            raise TimeoutError(
                f"CDP command timeout: {method}"
            )

        if "error" in msg:
            raise RuntimeError(
                f"{method}: {msg['error']}"
            )

        return msg.get("result", {})

    def evaluate(self, expression):
        return self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True
            }
        )

    def close(self):
        self.closed = True
        try:
            self.ws.close()
        except Exception:
            pass


class TikTokCollector:

    def __init__(
        self,
        cdp,
        run_dir,
        max_videos,
        search_scrolls,
        comment_scrolls,
        expand_replies=False,
    ):
        self.cdp = cdp
        self.run_dir = run_dir

        self.max_videos = max_videos
        self.search_scrolls = search_scrolls
        self.comment_scrolls = comment_scrolls
        self.expand_replies = expand_replies

        self.video_file = run_dir / "video_results.jsonl"
        self.comment_file = run_dir / "comments.jsonl"
        self.keyword_stat_file = run_dir / "keyword_stats.jsonl"
        self.checkpoint_file = run_dir / "checkpoint.json"

        self.raw_search_dir = run_dir / "raw" / "search"
        self.raw_comment_dir = run_dir / "raw" / "comments"

        self.raw_search_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        self.raw_comment_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        self.checkpoint = load_json(
            self.checkpoint_file,
            {
                "completed_keywords": [],
                "completed_comment_videos": []
            }
        )

        self.completed_keywords = set(
            self.checkpoint.get(
                "completed_keywords",
                []
            )
        )

        self.completed_comment_videos = set(
            self.checkpoint.get(
                "completed_comment_videos",
                []
            )
        )

        self.response_meta = {}
        self.body_pending = []

        self.current_keyword = None
        self.current_video = None

        self.search_has_more = None
        self.search_cursor = None

        self.comment_has_more = None
        self.comment_cursor = None
        self.comment_total = None

        self.keyword_to_videos = {}
        self.seen_keyword_video = set()
        self.seen_comments = set()

        self.new_search_videos = 0
        self.new_comments = 0

        # video_id -> 真实 /api/comment/list/ URL
        self.comment_templates = {}

        # 控制直接接口请求速度
        self.direct_comment_delay = 0.25
        self.direct_reply_delay = 0.25

        self._load_existing()

    def _load_existing(self):

        if self.video_file.exists():
            with self.video_file.open(
                "r",
                encoding="utf-8"
            ) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue

                    keyword = r.get("keyword")
                    vid = str(
                        r.get("video_id", "")
                    )

                    if not keyword or not vid:
                        continue

                    self.seen_keyword_video.add(
                        (keyword, vid)
                    )

                    self.keyword_to_videos.setdefault(
                        keyword,
                        []
                    )

                    if vid not in self.keyword_to_videos[keyword]:
                        self.keyword_to_videos[keyword].append(vid)

        if self.comment_file.exists():
            with self.comment_file.open(
                "r",
                encoding="utf-8"
            ) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue

                    vid = str(r.get("video_id", ""))
                    cid = str(r.get("comment_id", ""))

                    if vid and cid:
                        self.seen_comments.add(
                            (vid, cid)
                        )

    def save_checkpoint(self):
        self.checkpoint = {
            "completed_keywords":
                sorted(self.completed_keywords),

            "completed_comment_videos":
                sorted(self.completed_comment_videos),

            "updated_at":
                now()
        }

        save_json(
            self.checkpoint_file,
            self.checkpoint
        )

    def enable(self):
        self.cdp.send(
            "Network.enable",
            {
                "maxTotalBufferSize": 100_000_000,
                "maxResourceBufferSize": 50_000_000,
                "maxPostDataSize": 10_000_000
            }
        )

        self.cdp.send("Page.enable")
        self.cdp.send("Runtime.enable")

    def process_events(self):

        while True:
            try:
                msg = self.cdp.events.get_nowait()
            except queue.Empty:
                break

            method = msg.get("method")
            params = msg.get("params", {})

            if method == "Network.requestWillBeSent":

                request = params.get(
                    "request",
                    {}
                )

                url = request.get(
                    "url",
                    ""
                )

                # 保存当前视频真实 comment/list 请求，
                # 后面直接复用它的公共参数。
                if (
                    COMMENT_API in url
                    and REPLY_API not in url
                ):

                    vid = qparam(
                        url,
                        "aweme_id",
                        ""
                    )

                    if vid:

                        if not hasattr(
                            self,
                            "comment_templates"
                        ):
                            self.comment_templates = {}

                        self.comment_templates[
                            str(vid)
                        ] = url


            elif method == "Network.responseReceived":

                response = params.get(
                    "response",
                    {}
                )

                url = response.get(
                    "url",
                    ""
                )

                if (
                    SEARCH_API in url
                    or COMMENT_API in url
                    or REPLY_API in url
                ):
                    request_id = params.get(
                        "requestId"
                    )

                    self.response_meta[
                        request_id
                    ] = {
                        "url": url,
                        "status":
                            response.get("status"),
                        "mime":
                            response.get("mimeType", "")
                    }

            elif method == "Network.loadingFinished":

                request_id = params.get(
                    "requestId"
                )

                if request_id in self.response_meta:
                    self.body_pending.append(
                        request_id
                    )

            elif method == "Network.loadingFailed":

                request_id = params.get(
                    "requestId"
                )

                self.response_meta.pop(
                    request_id,
                    None
                )

    def process_bodies(self):

        pending = self.body_pending
        self.body_pending = []

        for request_id in pending:

            meta = self.response_meta.pop(
                request_id,
                None
            )

            if not meta:
                continue

            try:
                result = self.cdp.send(
                    "Network.getResponseBody",
                    {
                        "requestId":
                            request_id
                    },
                    timeout=10
                )

                body = result.get(
                    "body",
                    ""
                )

                obj = json.loads(body)

            except Exception:
                continue

            url = meta["url"]

            if SEARCH_API in url:
                self.handle_search(
                    url,
                    obj
                )

            elif REPLY_API in url:
                self.handle_replies(
                    url,
                    obj
                )

            elif COMMENT_API in url:
                self.handle_comments(
                    url,
                    obj
                )

    def drain(self, seconds):

        end = time.time() + seconds

        while time.time() < end:
            self.process_events()
            self.process_bodies()
            time.sleep(0.05)

        self.process_events()
        self.process_bodies()

    def normalize_video(self, item):

        author = item.get(
            "author",
            {}
        ) or {}

        stats = item.get(
            "stats",
            {}
        ) or {}

        video = item.get(
            "video",
            {}
        ) or {}

        music = item.get(
            "music",
            {}
        ) or {}

        vid = str(
            item.get("id", "")
        )

        unique_id = author.get(
            "uniqueId",
            ""
        )

        video_url = ""

        # TikTok 图文作品使用 /photo/，普通视频使用 /video/
        post_type = "video"

        image_post = item.get("imagePost")

        if image_post:
            post_type = "photo"

        if unique_id and vid:
            video_url = (
                f"https://www.tiktok.com/"
                f"@{unique_id}/{post_type}/{vid}"
            )

        return {
            "video_id": vid,

            "post_type":
                post_type,

            "desc":
                item.get("desc", ""),

            "create_time":
                item.get("createTime"),

            "author_id":
                author.get("id", ""),

            "author_unique_id":
                unique_id,

            "author_nickname":
                author.get("nickname", ""),

            "author_verified":
                author.get("verified"),

            "duration":
                video.get("duration"),

            "width":
                video.get("width"),

            "height":
                video.get("height"),

            "cover":
                video.get("cover", ""),

            "play_addr":
                video.get("playAddr", ""),

            "play_count":
                stats.get("playCount"),

            "digg_count":
                stats.get("diggCount"),

            "comment_count":
                stats.get("commentCount"),

            "share_count":
                stats.get("shareCount"),

            "collect_count":
                stats.get("collectCount"),

            "music_title":
                music.get("title", ""),

            "music_author":
                music.get("authorName", ""),

            "video_url":
                video_url,
        }

    def handle_search(self, url, obj):

        request_keyword = qparam(
            url,
            "keyword",
            ""
        )

        if (
            self.current_keyword
            and request_keyword
            and request_keyword != self.current_keyword
        ):
            return

        keyword = (
            request_keyword
            or self.current_keyword
            or ""
        )

        if not keyword:
            return

        self.search_cursor = obj.get(
            "cursor"
        )

        self.search_has_more = obj.get(
            "has_more"
        )

        raw_path = (
            self.raw_search_dir /
            (
                datetime.now().strftime(
                    "%Y%m%d_%H%M%S_%f"
                )
                + ".json"
            )
        )

        raw_path.write_text(
            json.dumps(
                obj,
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )

        for row in obj.get("data", []) or []:

            item = row.get(
                "item"
            ) if isinstance(row, dict) else None

            if not isinstance(item, dict):
                continue

            video = self.normalize_video(
                item
            )

            vid = video["video_id"]

            if not vid:
                continue

            key = (
                keyword,
                vid
            )

            self.keyword_to_videos.setdefault(
                keyword,
                []
            )

            if vid not in self.keyword_to_videos[keyword]:
                self.keyword_to_videos[keyword].append(
                    vid
                )

            if key in self.seen_keyword_video:
                continue

            self.seen_keyword_video.add(
                key
            )

            video["keyword"] = keyword
            video["collected_at"] = now()

            append_jsonl(
                self.video_file,
                video
            )

            self.new_search_videos += 1

            print(
                f"    + 视频 {vid} "
                f"| 播放 {video.get('play_count')} "
                f"| 评论 {video.get('comment_count')}"
            )

    def normalize_comment(
        self,
        video_id,
        c,
        parent_comment_id="",
        level=1,
    ):

        user = c.get(
            "user",
            {}
        ) or {}

        return {
            "video_id":
                str(video_id),

            "comment_id":
                str(
                    c.get("cid", "")
                ),

            "parent_comment_id":
                str(parent_comment_id or ""),

            "level":
                level,

            "text":
                c.get("text", ""),

            "create_time":
                c.get("create_time"),

            "digg_count":
                c.get("digg_count"),

            "reply_comment_total":
                c.get("reply_comment_total"),

            "user_id":
                user.get("uid", ""),

            "user_unique_id":
                user.get("unique_id", ""),

            "user_nickname":
                user.get("nickname", ""),

            "user_verified":
                user.get("verified"),

            "language":
                c.get("comment_language", ""),

            "collected_at":
                now(),
        }

    def save_comment_record(self, record):

        cid = record.get(
            "comment_id"
        )

        vid = record.get(
            "video_id"
        )

        if not cid or not vid:
            return

        key = (
            str(vid),
            str(cid)
        )

        if key in self.seen_comments:
            return

        self.seen_comments.add(
            key
        )

        append_jsonl(
            self.comment_file,
            record
        )

        self.new_comments += 1

    def handle_comments(self, url, obj):

        vid = qparam(
            url,
            "aweme_id",
            ""
        )

        if (
            self.current_video
            and vid
            and str(vid) != str(self.current_video)
        ):
            return

        vid = (
            vid
            or self.current_video
            or ""
        )

        if not vid:
            return

        self.comment_cursor = obj.get(
            "cursor"
        )

        self.comment_has_more = obj.get(
            "has_more"
        )

        self.comment_total = obj.get(
            "total"
        )

        raw_path = (
            self.raw_comment_dir /
            (
                f"{vid}_"
                + datetime.now().strftime(
                    "%Y%m%d_%H%M%S_%f"
                )
                + ".json"
            )
        )

        raw_path.write_text(
            json.dumps(
                obj,
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )

        for c in obj.get(
            "comments",
            []
        ) or []:

            self.save_comment_record(
                self.normalize_comment(
                    vid,
                    c,
                    level=1
                )
            )

    def handle_replies(self, url, obj):

        vid = qparam(
            url,
            "item_id",
            ""
        )

        parent_id = qparam(
            url,
            "comment_id",
            ""
        )

        if not vid:
            vid = self.current_video or ""

        if not vid:
            return

        for c in obj.get(
            "comments",
            []
        ) or []:

            self.save_comment_record(
                self.normalize_comment(
                    vid,
                    c,
                    parent_comment_id=parent_id,
                    level=2
                )
            )

    def navigate(self, url):

        print(
            f"\n打开：{url}"
        )

        self.cdp.send(
            "Page.navigate",
            {
                "url": url
            },
            timeout=15
        )

    def scroll_search(self):

        self.cdp.evaluate(
            """
            (() => {
                window.scrollBy(
                    0,
                    Math.max(
                        window.innerHeight * 0.9,
                        800
                    )
                );
                return window.scrollY;
            })()
            """
        )


    def open_comments_tab(self, retries=4):

        js = r"""
        (() => {

            const normalize = (s) =>
                (s || '')
                .replace(/\s+/g, ' ')
                .trim();

            const nodes = Array.from(
                document.querySelectorAll(
                    'button,[role="tab"],[role="button"],a,div,span'
                )
            );

            const vw = window.innerWidth;
            const vh = window.innerHeight;

            const candidates = [];

            for (const el of nodes) {

                const text = normalize(
                    el.innerText || el.textContent
                );

                const aria = normalize(
                    el.getAttribute('aria-label')
                );

                const low = text.toLowerCase();
                const ariaLow = aria.toLowerCase();

                const textHit =
                    /^comments?(?:\s+\d+)?$/i.test(text) ||
                    /^评论(?:\s*\d+)?$/.test(text) ||
                    /^評論(?:\s*\d+)?$/.test(text) ||
                    /^コメント(?:\s*\d+)?$/.test(text);

                const ariaHit =
                    ariaLow.includes('comment') ||
                    aria.includes('评论') ||
                    aria.includes('評論') ||
                    aria.includes('コメント');

                if (!textHit && !ariaHit) {
                    continue;
                }

                const rect =
                    el.getBoundingClientRect();

                const style =
                    window.getComputedStyle(el);

                if (
                    rect.width <= 0 ||
                    rect.height <= 0 ||
                    style.display === 'none' ||
                    style.visibility === 'hidden'
                ) {
                    continue;
                }

                let target =
                    el.closest(
                        'button,[role="tab"],[role="button"],a'
                    ) || el;

                const tr =
                    target.getBoundingClientRect();

                let score = 0;

                // TikTok 桌面端评论区一般位于右侧
                if (tr.left > vw * 0.45)
                    score += 20;

                if (
                    target.getAttribute('role') === 'tab'
                )
                    score += 15;

                if (
                    target.tagName === 'BUTTON'
                )
                    score += 10;

                if (textHit)
                    score += 10;

                if (
                    tr.top >= 0 &&
                    tr.top < vh * 0.85
                )
                    score += 5;

                candidates.push({
                    el: target,
                    text: text || aria,
                    score: score,
                    left: tr.left,
                    top: tr.top
                });
            }

            candidates.sort(
                (a, b) => b.score - a.score
            );

            if (!candidates.length) {

                return {
                    clicked: false,
                    reason: 'comments tab not found'
                };
            }

            const best = candidates[0];

            try {

                best.el.scrollIntoView({
                    block: 'center',
                    inline: 'nearest'
                });

            } catch(e) {}

            try {

                best.el.click();

            } catch(e) {

                try {
                    best.el.dispatchEvent(
                        new MouseEvent(
                            'click',
                            {
                                bubbles: true,
                                cancelable: true,
                                view: window
                            }
                        )
                    );
                } catch(e2) {

                    return {
                        clicked: false,
                        reason: String(e2)
                    };
                }
            }

            return {
                clicked: true,
                text: best.text,
                score: best.score,
                left: best.left,
                top: best.top
            };

        })()
        """

        for attempt in range(1, retries + 1):

            try:

                result = self.cdp.evaluate(
                    js
                )

                value = (
                    result
                    .get("result", {})
                    .get("value", {})
                )

                if isinstance(value, dict):

                    if value.get("clicked"):

                        print(
                            f"  已点击评论页签："
                            f"{value.get('text', 'Comments')}"
                        )

                    else:

                        print(
                            f"  第 {attempt} 次未找到评论页签"
                        )

            except Exception as e:

                print(
                    f"  点击 Comments 异常：{e}"
                )

            # 点击后监听 comment/list
            self.drain(2.5)

            if (
                self.comment_cursor is not None
                or self.comment_total is not None
            ):

                print(
                    f"  评论接口已触发"
                    f" | total={self.comment_total}"
                    f" | cursor={self.comment_cursor}"
                )

                return True

            time.sleep(0.5)

        return False



    def expand_visible_replies(self, max_clicks=20):

        js = r"""
        (() => {

            const maxClicks = MAX_CLICKS;

            const normalize = (s) =>
                (s || '')
                .replace(/\s+/g, ' ')
                .trim();

            const patterns = [
                /view\s+\d+\s+repl/i,
                /view\s+repl/i,
                /view\s+more\s+repl/i,
                /show\s+\d+\s+repl/i,
                /show\s+repl/i,
                /more\s+repl/i,

                /查看\s*\d*\s*条?\s*回复/i,
                /查看回复/i,
                /展开\s*\d*\s*条?\s*回复/i,
                /展开回复/i,
                /更多回复/i,

                /見る.*返信/i,
                /返信.*見る/i,
                /件の返信/i,

                /답글.*보기/i
            ];

            const nodes = Array.from(
                document.querySelectorAll(
                    'button,[role="button"],div,span'
                )
            );

            const candidates = [];

            for (const el of nodes) {

                const text = normalize(
                    el.innerText || el.textContent
                );

                if (!text)
                    continue;

                let hit = false;

                for (const rx of patterns) {
                    if (rx.test(text)) {
                        hit = true;
                        break;
                    }
                }

                if (!hit)
                    continue;

                const rect =
                    el.getBoundingClientRect();

                if (
                    rect.width <= 0 ||
                    rect.height <= 0
                ) {
                    continue;
                }

                if (
                    rect.bottom < 0 ||
                    rect.top > window.innerHeight
                ) {
                    continue;
                }

                const style =
                    window.getComputedStyle(el);

                if (
                    style.display === 'none' ||
                    style.visibility === 'hidden'
                ) {
                    continue;
                }

                const target =
                    el.closest(
                        'button,[role="button"]'
                    ) || el;

                candidates.push({
                    el: target,
                    text: text,
                    top: rect.top
                });
            }

            // 去重
            const unique = [];
            const seen = new Set();

            for (const c of candidates) {

                if (seen.has(c.el))
                    continue;

                seen.add(c.el);
                unique.push(c);
            }

            unique.sort(
                (a, b) => a.top - b.top
            );

            let clicked = 0;
            const labels = [];

            for (const c of unique) {

                if (clicked >= maxClicks)
                    break;

                try {

                    c.el.click();

                    clicked++;
                    labels.push(
                        c.text.slice(0, 80)
                    );

                } catch(e) {

                    try {

                        c.el.dispatchEvent(
                            new MouseEvent(
                                'click',
                                {
                                    bubbles: true,
                                    cancelable: true,
                                    view: window
                                }
                            )
                        );

                        clicked++;
                        labels.push(
                            c.text.slice(0, 80)
                        );

                    } catch(e2) {}
                }
            }

            return {
                clicked: clicked,
                labels: labels
            };

        })()
        """.replace(
            "MAX_CLICKS",
            str(max_clicks)
        )

        try:

            result = self.cdp.evaluate(
                js
            )

            value = (
                result
                .get("result", {})
                .get("value", {})
            )

            clicked = 0

            if isinstance(value, dict):

                clicked = int(
                    value.get(
                        "clicked",
                        0
                    ) or 0
                )

            if clicked:

                print(
                    f"  展开回复按钮："
                    f"{clicked} 个"
                )

                # 给 TikTok 时间请求 reply API
                self.drain(1.5)

            return clicked

        except Exception as e:

            print(
                f"  展开回复异常：{e}"
            )

            return 0


    def scroll_comments(self):

        js = r"""
        (() => {

            const vw =
                window.innerWidth;

            const vh =
                window.innerHeight;

            const candidates = [];

            const els =
                Array.from(
                    document.querySelectorAll(
                        'div,section,main'
                    )
                );

            for (const el of els) {

                try {

                    const r =
                        el.getBoundingClientRect();

                    const s =
                        window.getComputedStyle(el);

                    if (
                        r.width < 250 ||
                        r.height < 180
                    ) {
                        continue;
                    }

                    if (
                        r.right < vw * 0.55
                    ) {
                        continue;
                    }

                    if (
                        r.bottom <= 0 ||
                        r.top >= vh
                    ) {
                        continue;
                    }

                    if (
                        el.scrollHeight <=
                        el.clientHeight + 100
                    ) {
                        continue;
                    }

                    const overflow =
                        s.overflowY || '';

                    let score = 0;

                    if (
                        overflow === 'auto' ||
                        overflow === 'scroll'
                    ) {
                        score += 20;
                    }

                    if (
                        r.left > vw * 0.5
                    ) {
                        score += 20;
                    }

                    score += Math.min(
                        r.height / 100,
                        10
                    );

                    candidates.push({
                        el,
                        score,
                        left: r.left,
                        top: r.top,
                        height: r.height
                    });

                } catch(e) {}
            }

            candidates.sort(
                (a, b) =>
                    b.score - a.score
            );

            if (candidates.length) {

                const target =
                    candidates[0].el;

                const before =
                    target.scrollTop;

                target.scrollTop +=
                    Math.max(
                        target.clientHeight * 0.82,
                        500
                    );

                target.dispatchEvent(
                    new Event(
                        'scroll',
                        {
                            bubbles: true
                        }
                    )
                );

                return {
                    mode: 'comment-container',
                    before: before,
                    after: target.scrollTop,
                    scrollHeight:
                        target.scrollHeight,
                    clientHeight:
                        target.clientHeight,
                    candidates:
                        candidates.length
                };
            }

            // 没找到独立滚动容器时兜底
            window.scrollBy(
                0,
                Math.max(
                    vh * 0.75,
                    600
                )
            );

            return {
                mode: 'window'
            };

        })()
        """

        try:
            self.cdp.evaluate(
                js
            )
        except Exception:
            pass

        if self.expand_replies:

            self.cdp.evaluate(
                r"""
                (() => {

                    const rx =
                        /(view|show|more).{0,20}(repl|reply)|查看.{0,10}回复|展开.{0,10}回复|更多.{0,10}回复/i;

                    let count = 0;

                    const nodes =
                        document.querySelectorAll(
                            'button,[role="button"],div,span'
                        );

                    for (const el of nodes) {

                        const text =
                            (el.innerText || '')
                            .replace(/\s+/g, ' ')
                            .trim();

                        if (
                            text &&
                            rx.test(text)
                        ) {

                            try {

                                (
                                    el.closest(
                                        'button,[role="button"]'
                                    ) || el
                                ).click();

                                count++;

                            } catch(e) {}

                            if (count >= 10)
                                break;
                        }
                    }

                    return count;

                })()
                """
            )


    def search_keyword(self, keyword):

        self.current_keyword = keyword
        self.current_video = None

        self.search_has_more = None
        self.search_cursor = None

        start_count = len(
            self.keyword_to_videos.get(
                keyword,
                []
            )
        )

        self.new_search_videos = 0

        url = (
            "https://www.tiktok.com/search"
            "?q=" + quote(keyword)
        )

        print()
        print("=" * 80)
        print(
            f"搜索关键词：{keyword}"
        )
        print("=" * 80)

        self.navigate(url)

        self.drain(6)

        no_new_rounds = 0

        previous = len(
            self.keyword_to_videos.get(
                keyword,
                []
            )
        )

        for i in range(
            self.search_scrolls
        ):

            total_now = len(
                self.keyword_to_videos.get(
                    keyword,
                    []
                )
            )

            if (
                self.max_videos > 0
                and total_now >= self.max_videos
            ):
                print(
                    f"达到视频上限："
                    f"{self.max_videos}"
                )
                break

            if self.search_has_more == 0:
                print(
                    "搜索接口 has_more=0"
                )
                break

            try:
                self.scroll_search()
            except Exception:
                pass

            self.drain(1.8)

            current = len(
                self.keyword_to_videos.get(
                    keyword,
                    []
                )
            )

            print(
                f"\r搜索滚动 "
                f"{i + 1}/{self.search_scrolls} "
                f"| 已获取 {current} 个视频",
                end="",
                flush=True
            )

            if current == previous:
                no_new_rounds += 1
            else:
                no_new_rounds = 0
                previous = current

            if no_new_rounds >= 8:
                print(
                    "\n连续多次没有新视频，"
                    "结束当前关键词。"
                )
                break

        print()

        videos = self.keyword_to_videos.get(
            keyword,
            []
        )

        if self.max_videos > 0:
            videos = videos[
                :self.max_videos
            ]

        return videos


    def _direct_fetch_json(
        self,
        path
    ):

        js = f"""
        (async () => {{

            try {{

                const response = await fetch(
                    {json.dumps(path)},
                    {{
                        method: "GET",
                        credentials: "include",
                        headers: {{
                            "accept":
                            "application/json, text/plain, */*"
                        }}
                    }}
                );

                return {{
                    http_status:
                        response.status,

                    final_url:
                        response.url,

                    text:
                        await response.text()
                }};

            }} catch(e) {{

                return {{
                    error:
                        String(e)
                }};
            }}

        }})()
        """

        result = self.cdp.evaluate(
            js
        )

        value = (
            result
            .get("result", {})
            .get("value", {})
        )

        try:

            obj = json.loads(
                value.get(
                    "text",
                    ""
                )
            )

        except Exception:

            obj = {}

        return (
            value,
            obj
        )


    def _build_direct_path(
        self,
        template_url,
        endpoint,
        remove_keys,
        new_params
    ):

        parsed = urlparse(
            template_url
        )

        params = dict(
            parse_qsl(
                parsed.query,
                keep_blank_values=True
            )
        )

        # 去掉原接口专属参数
        for key in remove_keys:
            params.pop(
                key,
                None
            )

        # 去掉旧签名；
        # 浏览器页面 fetch 时 TikTok 会重新生成
        for key in [
            "X-Bogus",
            "X-Gnarly",
            "X-Dynosaur",
            "msToken",
        ]:
            params.pop(
                key,
                None
            )

        for key, value in new_params.items():

            params[
                key
            ] = str(
                value
            )

        return (
            endpoint
            + "?"
            + urlencode(
                params,
                doseq=True
            )
        )


    def get_comment_template(
        self,
        video_id,
        video_url
    ):

        video_id = str(
            video_id
        )

        existing = (
            self.comment_templates.get(
                video_id
            )
        )

        if existing:
            return existing


        # ----------------------------------------------------
        # 打开作品
        # ----------------------------------------------------

        print(
            f"\n打开作品：{video_url}"
        )

        self.navigate(
            video_url
        )

        self.drain(
            3
        )


        # 有些 photo 被历史数据存成 /video/
        # 如果页面疑似404，就尝试 /photo/
        try:

            check = self.cdp.evaluate(
                r"""
                (() => {
                    const text =
                        (document.body?.innerText || '')
                        .toLowerCase();

                    return (
                        text.includes(
                            "couldn't find this page"
                        )
                        ||
                        text.includes(
                            "could not find this page"
                        )
                    );
                })()
                """
            )

            is404 = (
                check
                .get("result", {})
                .get("value", False)
            )

        except Exception:

            is404 = False


        if (
            is404
            and "/video/" in video_url
        ):

            photo_url = (
                video_url.replace(
                    "/video/",
                    "/photo/",
                    1
                )
            )

            print(
                "  video 路径无效，尝试 photo："
            )

            print(
                f"  {photo_url}"
            )

            self.navigate(
                photo_url
            )

            self.drain(
                3
            )


        # ----------------------------------------------------
        # 页面有时自己已经发出 comment/list
        # ----------------------------------------------------

        template = (
            self.comment_templates.get(
                video_id
            )
        )

        if template:

            print(
                "  已自动捕获 comment/list 模板"
            )

            return template


        # ----------------------------------------------------
        # 自动点击 Comments
        # 只需要触发一次父评论接口
        # ----------------------------------------------------

        print(
            "  正在切换到 Comments..."
        )

        for attempt in range(
            1,
            6
        ):

            try:

                self.open_comments_tab(
                    retries=1
                )

            except Exception:
                pass


            self.drain(
                2
            )


            template = (
                self.comment_templates.get(
                    video_id
                )
            )

            if template:

                print(
                    f"  comment/list 模板已捕获"
                    f"（第 {attempt} 次）"
                )

                return template


            print(
                f"  第 {attempt} 次"
                f"尚未捕获 comment/list"
            )


        print(
            "  [!] 无法获取 comment/list 模板"
        )

        return None


    def fetch_all_parent_comments(
        self,
        template_url,
        video_id
    ):

        video_id = str(
            video_id
        )

        all_comments = []

        seen_ids = set()

        cursor = 0

        page = 0

        api_total = None


        # 从真实请求中继承 count，
        # 没有就默认20
        try:

            original_count = int(
                qparam(
                    template_url,
                    "count",
                    "20"
                )
                or 20
            )

        except Exception:

            original_count = 20


        while True:

            page += 1


            path = (
                self._build_direct_path(
                    template_url=
                        template_url,

                    endpoint=
                        "/api/comment/list/",

                    remove_keys=[
                        "aweme_id",
                        "cursor",
                        "count",
                    ],

                    new_params={
                        "aweme_id":
                            video_id,

                        "cursor":
                            cursor,

                        "count":
                            original_count,
                    }
                )
            )


            value, obj = (
                self._direct_fetch_json(
                    path
                )
            )


            http_status = (
                value.get(
                    "http_status"
                )
            )

            status_code = (
                obj.get(
                    "status_code"
                )
            )


            if (
                http_status != 200
                or status_code != 0
            ):

                print(
                    f"  ❌ 父评论第 {page} 页失败"
                    f" | HTTP={http_status}"
                    f" | status_code={status_code}"
                )

                break


            rows = (
                obj.get(
                    "comments",
                    []
                )
                or []
            )


            next_cursor = (
                obj.get(
                    "cursor",
                    cursor
                )
            )

            has_more = (
                obj.get(
                    "has_more",
                    0
                )
            )

            api_total = (
                obj.get(
                    "total"
                )
            )


            new_count = 0


            for comment in rows:

                cid = str(
                    comment.get(
                        "cid"
                    )
                    or ""
                )

                if not cid:
                    continue


                if cid in seen_ids:
                    continue


                seen_ids.add(
                    cid
                )

                all_comments.append(
                    comment
                )

                new_count += 1


                # 直接保存一级评论
                self.save_comment_record(
                    self.normalize_comment(
                        video_id,
                        comment,
                        level=1
                    )
                )


            print(
                f"  父评论页 {page}"
                f" | 本页={len(rows)}"
                f" | 新增={new_count}"
                f" | 累计={len(all_comments)}"
                f" | total={api_total}"
                f" | cursor={next_cursor}"
                f" | has_more={has_more}"
            )


            if not has_more:
                break


            try:

                current_cursor = int(
                    cursor
                )

                next_cursor_int = int(
                    next_cursor
                )

            except Exception:

                print(
                    "  ⚠ 父评论 cursor 无法解析"
                )

                break


            if (
                next_cursor_int
                <= current_cursor
            ):

                print(
                    "  ⚠ 父评论 cursor 没有前进"
                )

                break


            cursor = (
                next_cursor_int
            )


            time.sleep(
                self.direct_comment_delay
            )


        return (
            all_comments,
            api_total
        )


    def fetch_all_replies_direct(
        self,
        template_url,
        video_id,
        parent_comment
    ):

        video_id = str(
            video_id
        )

        comment_id = str(
            parent_comment.get(
                "cid"
            )
            or ""
        )


        if not comment_id:
            return []


        try:

            expected_total = int(
                parent_comment.get(
                    "reply_comment_total"
                )
                or 0
            )

        except Exception:

            expected_total = 0


        if expected_total <= 0:
            return []


        all_replies = []

        seen_ids = set()

        cursor = 0

        page = 0


        while True:

            page += 1


            path = (
                self._build_direct_path(
                    template_url=
                        template_url,

                    endpoint=
                        "/api/comment/list/reply/",

                    remove_keys=[
                        "aweme_id",
                        "item_id",
                        "comment_id",
                        "cursor",
                        "count",
                    ],

                    new_params={
                        "item_id":
                            video_id,

                        "comment_id":
                            comment_id,

                        "cursor":
                            cursor,

                        # 真实页面常用3
                        "count":
                            3,
                    }
                )
            )


            value, obj = (
                self._direct_fetch_json(
                    path
                )
            )


            http_status = (
                value.get(
                    "http_status"
                )
            )

            status_code = (
                obj.get(
                    "status_code"
                )
            )


            if (
                http_status != 200
                or status_code != 0
            ):

                print(
                    f"      ❌ 回复页 {page} 失败"
                    f" | HTTP={http_status}"
                    f" | status_code={status_code}"
                )

                break


            replies = (
                obj.get(
                    "comments",
                    []
                )
                or []
            )


            next_cursor = (
                obj.get(
                    "cursor",
                    cursor
                )
            )

            has_more = (
                obj.get(
                    "has_more",
                    0
                )
            )

            api_total = (
                obj.get(
                    "total"
                )
            )


            new_count = 0


            for reply in replies:

                rid = str(
                    reply.get(
                        "cid"
                    )
                    or ""
                )


                if not rid:
                    continue


                if rid in seen_ids:
                    continue


                seen_ids.add(
                    rid
                )

                all_replies.append(
                    reply
                )

                new_count += 1


                self.save_comment_record(
                    self.normalize_comment(
                        video_id,
                        reply,
                        parent_comment_id=
                            comment_id,
                        level=2
                    )
                )


            print(
                f"      reply页 {page}"
                f" | 本页={len(replies)}"
                f" | 新增={new_count}"
                f" | 累计={len(all_replies)}"
                f" | total={api_total}"
                f" | cursor={next_cursor}"
                f" | has_more={has_more}"
            )


            if not has_more:
                break


            try:

                current_cursor = int(
                    cursor
                )

                next_cursor_int = int(
                    next_cursor
                )

            except Exception:

                break


            if (
                next_cursor_int
                <= current_cursor
            ):

                break


            cursor = (
                next_cursor_int
            )


            time.sleep(
                self.direct_reply_delay
            )


        if (
            expected_total > 0
            and len(all_replies)
            < expected_total
        ):

            print(
                f"      ⚠ 前台记录回复="
                f"{expected_total}"
                f"，接口当前可返回="
                f"{len(all_replies)}"
            )


        return all_replies


    def collect_video_comments(
        self,
        video_id,
        video_url
    ):

        video_id = str(
            video_id
        )


        if (
            video_id
            in self.completed_comment_videos
        ):

            print(
                f"  评论已完成，跳过："
                f"{video_id}"
            )

            return


        self.current_video = (
            video_id
        )

        self.current_keyword = (
            None
        )


        print()
        print(
            "-" * 80
        )

        print(
            f"采集作品评论："
            f"{video_id}"
        )

        print(
            "-" * 80
        )


        # ----------------------------------------------------
        # 1. 获取真实 comment/list 请求模板
        # ----------------------------------------------------

        template = (
            self.get_comment_template(
                video_id,
                video_url
            )
        )


        if not template:

            print(
                "  [!] 没有拿到父评论接口模板，"
                "当前作品不标记完成。"
            )

            return


        # ----------------------------------------------------
        # 2. 直接分页获取全部一级评论
        # ----------------------------------------------------

        print()
        print(
            "  开始直接分页获取一级评论..."
        )


        parents, api_total = (
            self.fetch_all_parent_comments(
                template,
                video_id
            )
        )


        print()
        print(
            f"  一级评论获取完成："
            f"{len(parents)}"
            f" | API total={api_total}"
        )


        # ----------------------------------------------------
        # 3. 找所有有回复的一级评论
        # ----------------------------------------------------

        reply_parents = []


        for comment in parents:

            try:

                total = int(
                    comment.get(
                        "reply_comment_total"
                    )
                    or 0
                )

            except Exception:

                total = 0


            if total > 0:

                reply_parents.append(
                    comment
                )


        print(
            f"  有二级回复的父评论："
            f"{len(reply_parents)}"
        )


        # ----------------------------------------------------
        # 4. 每条父评论直接分页获取全部回复
        #
        # 不再点击 View replies
        # ----------------------------------------------------

        total_replies = 0


        for index, parent in enumerate(
            reply_parents,
            1
        ):

            cid = str(
                parent.get(
                    "cid"
                )
                or ""
            )


            try:

                expected = int(
                    parent.get(
                        "reply_comment_total"
                    )
                    or 0
                )

            except Exception:

                expected = 0


            print(
                f"\n    [{index}/"
                f"{len(reply_parents)}]"
                f" 父评论 {cid}"
                f" | reply_total={expected}"
            )


            replies = (
                self.fetch_all_replies_direct(
                    template,
                    video_id,
                    parent
                )
            )


            total_replies += len(
                replies
            )


        # ----------------------------------------------------
        # 5. 汇总
        # ----------------------------------------------------

        print()
        print(
            "=" * 70
        )

        print(
            f"作品 {video_id} 评论采集完成"
        )

        print(
            f"一级评论："
            f"{len(parents)}"
        )

        print(
            f"二级回复："
            f"{total_replies}"
        )

        print(
            f"合计："
            f"{len(parents) + total_replies}"
        )

        print(
            f"TikTok API total："
            f"{api_total}"
        )

        print(
            "=" * 70
        )


        # ----------------------------------------------------
        # 6. 标记完成
        # ----------------------------------------------------

        self.completed_comment_videos.add(
            video_id
        )

        self.save_checkpoint()



    def get_video_records(
        self,
        keyword
    ):

        result = {}

        if not self.video_file.exists():
            return result

        with self.video_file.open(
            "r",
            encoding="utf-8"
        ) as f:

            for line in f:

                try:
                    r = json.loads(
                        line
                    )
                except Exception:
                    continue

                if (
                    r.get("keyword")
                    != keyword
                ):
                    continue

                vid = str(
                    r.get(
                        "video_id",
                        ""
                    )
                )

                if vid:
                    result[vid] = r

        return result

    def run_keyword(self, keyword):

        if keyword in self.completed_keywords:
            print(
                f"\n已完成关键词，跳过："
                f"{keyword}"
            )
            return

        videos = self.search_keyword(
            keyword
        )

        records = self.get_video_records(
            keyword
        )

        for index, vid in enumerate(
            videos,
            1
        ):

            record = records.get(
                str(vid),
                {}
            )

            video_url = record.get(
                "video_url",
                ""
            )

            if not video_url:
                continue

            print(
                f"\n[{index}/{len(videos)}] "
                f"{video_url}"
            )

            self.collect_video_comments(
                str(vid),
                video_url
            )

        self.completed_keywords.add(
            keyword
        )

        self.save_checkpoint()

        append_jsonl(
            self.keyword_stat_file,
            {
                "keyword":
                    keyword,

                "video_count":
                    len(videos),

                "completed_at":
                    now()
            }
        )


def export_excel(run_dir):

    try:
        from openpyxl import Workbook
    except Exception:
        print(
            "\n未安装 openpyxl，"
            "跳过 Excel 输出。"
        )
        return

    video_file = (
        run_dir /
        "video_results.jsonl"
    )

    comment_file = (
        run_dir /
        "comments.jsonl"
    )

    if (
        not video_file.exists()
        and not comment_file.exists()
    ):
        return

    output = (
        run_dir /
        "TikTok采集结果.xlsx"
    )

    wb = Workbook()
    ws = wb.active
    ws.title = "视频信息"

    video_headers = [
        "关键词",
        "视频ID",
        "视频链接",
        "视频文案",
        "发布时间",
        "作者账号",
        "作者昵称",
        "作者ID",
        "播放量",
        "点赞量",
        "评论量",
        "收藏量",
        "分享量",
        "视频时长",
        "宽度",
        "高度",
        "封面",
        "音乐标题",
        "音乐作者",
        "采集时间",
    ]

    ws.append(video_headers)

    if video_file.exists():

        merged = {}

        with video_file.open(
            "r",
            encoding="utf-8"
        ) as f:

            for line in f:

                try:
                    r = json.loads(
                        line
                    )
                except Exception:
                    continue

                vid = str(
                    r.get(
                        "video_id",
                        ""
                    )
                )

                if not vid:
                    continue

                if vid not in merged:
                    merged[vid] = {
                        **r,
                        "_keywords": set()
                    }

                merged[vid][
                    "_keywords"
                ].add(
                    r.get(
                        "keyword",
                        ""
                    )
                )

        for vid, r in merged.items():

            keywords = "、".join(
                sorted(
                    x for x
                    in r["_keywords"]
                    if x
                )
            )

            ws.append([
                keywords,
                vid,
                r.get("video_url"),
                r.get("desc"),
                r.get("create_time"),
                r.get("author_unique_id"),
                r.get("author_nickname"),
                r.get("author_id"),
                r.get("play_count"),
                r.get("digg_count"),
                r.get("comment_count"),
                r.get("collect_count"),
                r.get("share_count"),
                r.get("duration"),
                r.get("width"),
                r.get("height"),
                r.get("cover"),
                r.get("music_title"),
                r.get("music_author"),
                r.get("collected_at"),
            ])

    wc = wb.create_sheet(
        "评论明细"
    )

    comment_headers = [
        "视频ID",
        "评论ID",
        "父评论ID",
        "层级",
        "评论内容",
        "评论时间",
        "点赞数",
        "回复数",
        "用户账号",
        "用户昵称",
        "用户ID",
        "语言",
        "采集时间",
    ]

    wc.append(
        comment_headers
    )

    if comment_file.exists():

        with comment_file.open(
            "r",
            encoding="utf-8"
        ) as f:

            for line in f:

                try:
                    r = json.loads(
                        line
                    )
                except Exception:
                    continue

                wc.append([
                    r.get("video_id"),
                    r.get("comment_id"),
                    r.get("parent_comment_id"),
                    r.get("level"),
                    r.get("text"),
                    r.get("create_time"),
                    r.get("digg_count"),
                    r.get("reply_comment_total"),
                    r.get("user_unique_id"),
                    r.get("user_nickname"),
                    r.get("user_id"),
                    r.get("language"),
                    r.get("collected_at"),
                ])

    # 简单列宽
    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["B"].width = 23
    ws.column_dimensions["C"].width = 55
    ws.column_dimensions["D"].width = 80
    ws.column_dimensions["F"].width = 25
    ws.column_dimensions["G"].width = 25

    wc.column_dimensions["A"].width = 23
    wc.column_dimensions["B"].width = 23
    wc.column_dimensions["E"].width = 90
    wc.column_dimensions["I"].width = 25
    wc.column_dimensions["J"].width = 25

    ws.freeze_panes = "A2"
    wc.freeze_panes = "A2"

    ws.auto_filter.ref = ws.dimensions
    wc.auto_filter.ref = wc.dimensions

    wb.save(
        output
    )

    print(
        f"\nExcel 已输出：{output}"
    )


def find_profile(
    api_key,
    debug_port=None
):

    if debug_port:
        target = find_tiktok_target(
            debug_port
        )

        return (
            str(debug_port),
            target
        )

    browsers = get_active_browsers(
        api_key
    )

    for b in browsers:

        port = b.get(
            "debug_port"
        )

        if not port:
            ws = b.get(
                "ws",
                {}
            ) or {}

            selenium = ws.get(
                "selenium",
                ""
            )

            if ":" in selenium:
                port = selenium.rsplit(
                    ":",
                    1
                )[-1]

        if not port:
            continue

        try:
            target = find_tiktok_target(
                port
            )
        except Exception:
            continue

        if (
            "tiktok.com"
            in target.get(
                "url",
                ""
            ).lower()
        ):
            print(
                f"AdsPower环境："
                f"{b.get('user_id')}"
            )

            return (
                str(port),
                target
            )

    raise RuntimeError(
        "没有找到已打开 TikTok 的 AdsPower 环境。"
    )


def load_keywords(path):

    if not path.exists():
        path.write_text(
            "鼠标\n键盘\n",
            encoding="utf-8"
        )

        print(
            f"已创建关键词文件：{path}"
        )
        print(
            "请修改关键词后重新运行。"
        )

        return []

    result = []

    for line in path.read_text(
        encoding="utf-8-sig"
    ).splitlines():

        k = line.strip()

        if (
            k
            and not k.startswith("#")
            and k not in result
        ):
            result.append(k)

    return result


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--keywords",
        default=str(ROOT / "keywords.txt")
    )

    parser.add_argument(
        "--debug-port",
        default=""
    )

    parser.add_argument(
        "--max-videos",
        type=int,
        default=50
    )

    parser.add_argument(
        "--search-scrolls",
        type=int,
        default=60
    )

    parser.add_argument(
        "--comment-scrolls",
        type=int,
        default=120
    )

    parser.add_argument(
        "--expand-replies",
        action="store_true"
    )

    parser.add_argument(
        "--resume",
        default=""
    )

    args = parser.parse_args()

    keywords = load_keywords(
        Path(args.keywords)
    )

    if not keywords:
        return

    api_key = os.getenv(
        "ADSPOWER_API_KEY",
        ""
    ).strip()

    if args.resume:

        run_dir = Path(
            args.resume
        )

        run_dir.mkdir(
            parents=True,
            exist_ok=True
        )

    else:

        run_dir = (
            RUN_ROOT /
            datetime.now().strftime(
                "%Y%m%d_%H%M%S"
            )
        )

        run_dir.mkdir(
            parents=True,
            exist_ok=True
        )

    print()
    print("=" * 80)
    print("TikTok AdsPower 正式采集器")
    print("=" * 80)
    print(f"输出目录：{run_dir}")
    print(f"关键词数：{len(keywords)}")
    print(f"每关键词视频上限：{args.max_videos}")
    print()

    debug_port, target = find_profile(
        api_key=api_key,
        debug_port=args.debug_port or None
    )

    print(
        f"DebugPort：{debug_port}"
    )
    print(
        f"Target：{target.get('id')}"
    )
    print(
        f"当前URL：{target.get('url')}"
    )

    cdp = CDP(
        target[
            "webSocketDebuggerUrl"
        ]
    )

    collector = TikTokCollector(
        cdp=cdp,
        run_dir=run_dir,
        max_videos=args.max_videos,
        search_scrolls=args.search_scrolls,
        comment_scrolls=args.comment_scrolls,
        expand_replies=args.expand_replies,
    )

    collector.enable()

    try:

        for i, keyword in enumerate(
            keywords,
            1
        ):

            print()
            print(
                f"\n######## "
                f"关键词 {i}/{len(keywords)} "
                f"########"
            )

            collector.run_keyword(
                keyword
            )

    except KeyboardInterrupt:

        print(
            "\n\n用户停止采集。"
        )

        collector.save_checkpoint()

    finally:

        try:
            collector.drain(1)
        except Exception:
            pass

        cdp.close()

        export_excel(
            run_dir
        )

        print()
        print("=" * 80)
        print("采集结束")
        print("=" * 80)
        print(
            f"结果目录：{run_dir}"
        )
        print(
            f"视频：{run_dir / 'video_results.jsonl'}"
        )
        print(
            f"评论：{run_dir / 'comments.jsonl'}"
        )
        print(
            f"断点：{run_dir / 'checkpoint.json'}"
        )


if __name__ == "__main__":
    main()
