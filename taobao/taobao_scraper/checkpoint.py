"""断点续抓。

淘宝分页依赖上一页返回的 paging_state，失败页不能跳过，
因此断点必须记录"下一页页码 + 分页状态 + 已抓商品"。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from .config import Settings, initial_paging_state
from .utils import atomic_write_json, safe_filename


def checkpoint_path(keyword: str, settings: Settings) -> Path:
    return settings.checkpoint_dir / f"{safe_filename(keyword)}.json"


def new_checkpoint(keyword: str) -> dict:
    return {
        "keyword": keyword,
        "next_page": 1,
        "paging_state": initial_paging_state(),
        "products": [],
        "completed": False,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def load_checkpoint(keyword: str, settings: Settings) -> dict:
    path = checkpoint_path(keyword, settings)
    if settings.force_refresh or not settings.resume or not path.exists():
        return new_checkpoint(keyword)

    try:
        import json

        with open(path, "r", encoding="utf-8") as f:
            cp = json.load(f)
        if cp.get("keyword") != keyword:
            return new_checkpoint(keyword)
        cp.setdefault("next_page", 1)
        cp.setdefault("paging_state", initial_paging_state())
        cp.setdefault("products", [])
        cp.setdefault("completed", False)
        logging.info(
            f"[{keyword}] 恢复断点：next_page={cp['next_page']}，"
            f"已有商品={len(cp['products'])}"
        )
        return cp
    except Exception as e:
        logging.warning(f"[{keyword}] 断点读取失败，从头开始: {e}")
        return new_checkpoint(keyword)


def save_checkpoint(cp: dict, settings: Settings) -> None:
    cp["updated_at"] = datetime.now().isoformat(timespec="seconds")
    atomic_write_json(checkpoint_path(cp["keyword"], settings), cp)


def visible_products(cp: dict, settings: Settings) -> list[dict]:
    """取断点中 page <= max_pages 的商品，并重排总排名。

    断点里可能留有比本次目标页数更多的数据；输出只取本次范围内的部分，
    但不会删除旧断点中的多余数据，方便下次提高页数时衔接。
    """
    products = [
        x
        for x in (cp.get("products") or [])
        if int(x.get("page") or 0) <= settings.max_pages
    ]
    for idx, p in enumerate(products, start=1):
        p["global_rank"] = idx
    return products
