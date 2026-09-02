"""主图下载与压缩。

两个关键点：
1. Session 按工作线程复用。原实现在每次重试内新建 Session，
   等于每个请求都重新做 TCP + TLS 握手，连接池完全没有意义，
   这也是图片 CDN 频繁 TLS EOF 的真正原因之一。
2. 单张图有总时间预算。候选 CDN 最多 6 个、每个最多重试 4 次，
   全部跑满最坏要 100s+，少数失效图片就能把整个线程池拖住。
"""

from __future__ import annotations

import io
import logging
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from PIL import Image as PILImage

from .config import Settings
from .utils import safe_filename

# Alibaba 图片对象通常可以跨这些 CDN 域名访问。
# 优先 img/gw，再轮询其它 g-search 节点。
FALLBACK_HOSTS = (
    "img.alicdn.com",
    "gw.alicdn.com",
    "g-search1.alicdn.com",
    "g-search2.alicdn.com",
    "g-search3.alicdn.com",
)

_worker_state = threading.local()


def build_image_candidate_urls(image_url: str) -> list[str]:
    """
    为淘宝/阿里 CDN 主图生成兜底地址。

    搜索接口里的 pic_path 偶尔会指向已经失效的 g-search 节点并返回 404。
    同一对象往往仍可从 img.alicdn.com / gw.alicdn.com 或其它 g-search 节点取得。
    404 属于"地址失效"，因此不应该对同一个 URL 重复重试 4 次，而应立即切换候选 CDN。
    """
    if not image_url:
        return []

    url = str(image_url).strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("http://"):
        url = "https://" + url[len("http://") :]

    candidates: list[str] = []

    def add(u: str) -> None:
        if u and u not in candidates:
            candidates.append(u)

    add(url)

    m = re.match(r"^https://([^/]+)(/.*)$", url, flags=re.I)
    if not m:
        return candidates

    host = m.group(1).lower()
    path = m.group(2)

    if host.endswith("alicdn.com"):
        for fallback_host in FALLBACK_HOSTS:
            if fallback_host != host:
                add(f"https://{fallback_host}{path}")

    return candidates


def _make_image_session(user_agent: str, settings: Settings) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Referer": "https://s.taobao.com/",
            "Accept": (
                "image/avif,image/webp,image/apng,image/svg+xml,"
                "image/*,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            # 注意：不要设 Connection: close，否则连接池复用失效，
            # 每个请求都要重新握手，反而更容易触发 CDN 的 TLS EOF。
            "Connection": "keep-alive",
        }
    )
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=4,
        pool_maxsize=8,
        max_retries=0,
        pool_block=False,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _worker_init(user_agent: str, settings: Settings) -> None:
    _worker_state.session = _make_image_session(user_agent, settings)
    _worker_state.settings = settings


def _get_session(user_agent: str, settings: Settings) -> requests.Session:
    session = getattr(_worker_state, "session", None)
    if session is None:
        session = _make_image_session(user_agent, settings)
        _worker_state.session = session
    return session


def _save_thumbnail(content: bytes, out_path: Path, settings: Settings) -> None:
    with PILImage.open(io.BytesIO(content)) as im:
        if im.mode in {"RGBA", "LA"}:
            bg = PILImage.new("RGB", im.size, "white")
            alpha = im.getchannel("A") if "A" in im.getbands() else None
            bg.paste(im.convert("RGBA"), mask=alpha)
            im = bg
        else:
            im = im.convert("RGB")

        im.thumbnail(
            (settings.image_max_size, settings.image_max_size),
            PILImage.Resampling.LANCZOS,
        )

        canvas = PILImage.new(
            "RGB", (settings.image_max_size, settings.image_max_size), "white"
        )
        x = (settings.image_max_size - im.width) // 2
        y = (settings.image_max_size - im.height) // 2
        canvas.paste(im, (x, y))

        tmp_path = out_path.with_suffix(".tmp.jpg")
        canvas.save(
            tmp_path,
            "JPEG",
            quality=settings.image_jpeg_quality,
            optimize=True,
            progressive=True,
        )
        os.replace(tmp_path, out_path)


def prepare_image_file(
    product: dict,
    target_dir: Path,
    user_agent: str,
    settings: Settings,
) -> tuple[str, str]:
    """下载并压缩一张主图，返回 (item_id, 本地路径)。失败时路径为空字符串。"""
    item_id = product.get("item_id", "")
    image_url = product.get("image_url", "")
    if not item_id or not image_url:
        return item_id, ""

    candidate_urls = build_image_candidate_urls(image_url)
    if not candidate_urls:
        return item_id, ""

    out_path = target_dir / f"{item_id}.jpg"
    if out_path.exists() and out_path.stat().st_size > 0:
        return item_id, str(out_path)

    session = _get_session(user_agent, settings)
    deadline = time.monotonic() + settings.image_total_timeout

    last_error: Exception | None = None
    all_404 = True
    timed_out = False

    # 404：立即换 CDN，不在同一个失效 URL 上浪费重试时间。
    # TLS/Connection/Timeout：仍按 image_download_retries 做指数退避。
    for candidate_index, candidate_url in enumerate(candidate_urls, start=1):
        if time.monotonic() >= deadline:
            timed_out = True
            break

        for attempt in range(1, settings.image_download_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break

            try:
                if attempt == 1:
                    time.sleep(random.uniform(0.03, 0.20))

                r = session.get(
                    candidate_url,
                    timeout=(
                        settings.image_connect_timeout,
                        settings.image_read_timeout,
                    ),
                    allow_redirects=True,
                )

                if r.status_code == 404:
                    last_error = requests.exceptions.HTTPError(
                        f"404 Not Found: {candidate_url}"
                    )
                    logging.debug(
                        f"主图 CDN 地址 404，切换候选地址 item_id={item_id} "
                        f"candidate={candidate_index}/{len(candidate_urls)} "
                        f"url={candidate_url}"
                    )
                    # 404 是确定性结果，不重复请求同一个 URL。
                    break

                all_404 = False
                r.raise_for_status()

                content_type = (r.headers.get("Content-Type") or "").lower()
                if content_type and "image" not in content_type:
                    raise ValueError(
                        f"返回内容不是图片: status={r.status_code}, "
                        f"content-type={content_type}"
                    )

                if len(r.content) < 128:
                    raise ValueError(f"图片响应过小: {len(r.content)} bytes")

                _save_thumbnail(r.content, out_path, settings)

                if candidate_index > 1:
                    logging.info(
                        f"主图原地址不可用，已通过备用 CDN 恢复 item_id={item_id} "
                        f"cdn={candidate_url.split('/')[2]}"
                    )

                return item_id, str(out_path)

            except Exception as e:
                last_error = e
                all_404 = False

                try:
                    tmp_path = out_path.with_suffix(".tmp.jpg")
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    pass

                # HTTP 404 已在上方 break，不会走这里。
                if attempt < settings.image_download_retries:
                    delay = (
                        settings.image_retry_base_delay * (2 ** (attempt - 1))
                    ) + random.uniform(0.2, 0.9)

                    # 退避时间超过剩余预算就直接放弃，不再白等。
                    if delay >= remaining:
                        timed_out = True
                        break

                    logging.debug(
                        f"主图下载重试 item_id={item_id} "
                        f"candidate={candidate_index}/{len(candidate_urls)} "
                        f"attempt={attempt}/{settings.image_download_retries} "
                        f"wait={delay:.1f}s error={e}"
                    )
                    time.sleep(delay)

        if timed_out:
            break

    if timed_out:
        logging.warning(
            f"主图超出单图时间预算 {settings.image_total_timeout:.0f}s，"
            f"放弃 item_id={item_id}；已尝试 {candidate_index} 个 CDN 地址。"
            f"Excel 将保留主图URL，但该行不嵌入图片"
        )
    elif all_404:
        logging.warning(
            f"主图已失效 item_id={item_id}：原地址及 "
            f"{len(candidate_urls)-1} 个备用 CDN 均返回 404；"
            f"Excel 将保留主图URL，但该行不嵌入图片"
        )
    else:
        logging.warning(
            f"主图最终下载失败 item_id={item_id}，"
            f"已尝试 {len(candidate_urls)} 个 CDN 地址；最后错误: {last_error}"
        )

    return item_id, ""


def download_keyword_images(
    keyword: str,
    products: list[dict],
    user_agent: str,
    settings: Settings,
) -> dict[str, str]:
    """并发下载一个关键词的全部主图，返回 item_id -> 本地路径。"""
    if not settings.embed_images or not products:
        return {}

    target_dir = settings.image_cache_dir / safe_filename(keyword)
    target_dir.mkdir(parents=True, exist_ok=True)

    logging.info(
        f"[{keyword}] 开始并发下载/压缩 {len(products)} 张主图，"
        f"workers={settings.image_workers}，"
        f"单图最多重试={settings.image_download_retries}次，"
        f"单图时间预算={settings.image_total_timeout:.0f}s"
    )

    result: dict[str, str] = {}
    workers = max(1, min(settings.image_workers, len(products)))

    with ThreadPoolExecutor(
        max_workers=workers,
        initializer=_worker_init,
        initargs=(user_agent, settings),
    ) as executor:
        futures = [
            executor.submit(prepare_image_file, p, target_dir, user_agent, settings)
            for p in products
        ]

        completed = 0
        failed = 0
        for future in as_completed(futures):
            # 单张图失败不能影响整批导出，因此在这里隔离异常。
            try:
                item_id, path = future.result()
            except Exception as e:
                failed += 1
                logging.warning(f"[{keyword}] 主图处理线程异常: {e}")
                completed += 1
                continue

            if item_id:
                result[item_id] = path
            completed += 1
            if completed % 50 == 0 or completed == len(products):
                logging.info(f"[{keyword}] 图片处理 {completed}/{len(products)}")

    ok_count = sum(1 for x in result.values() if x)
    if failed:
        logging.warning(f"[{keyword}] 图片线程异常 {failed} 张")
    logging.info(f"[{keyword}] 图片可用 {ok_count}/{len(products)}")
    return result
