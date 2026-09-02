"""单个关键词的分页抓取流程。"""

from __future__ import annotations

import logging

import requests

from . import cooling
from .checkpoint import load_checkpoint, save_checkpoint, visible_products
from .config import Settings
from .errors import NetworkTransportError, RiskLimitError
from .extract import extract_products
from .mtop import call_mtop_search, update_paging_state
from .utils import atomic_write_json, safe_filename

# 循环结束的原因，决定断点里 next_page 怎么记
STOP_COMPLETED = "completed"
STOP_EMPTY = "empty_pages"
STOP_BEYOND_TOTAL = "beyond_total_page"


def resolve_total_page(paging_state: dict, settings: Settings) -> int:
    try:
        return int(paging_state.get("totalPage") or settings.max_pages)
    except Exception:
        return settings.max_pages


def scrape_keyword(
    session: requests.Session,
    environment: dict,
    keyword: str,
    settings: Settings,
) -> tuple[list[dict], dict, bool]:
    """抓取一个关键词，返回 (商品列表, environment, 是否完成)。

    environment 可能因 token 刷新被替换，需要回传给调用方。
    """
    cp = load_checkpoint(keyword, settings)
    products = visible_products(cp, settings)

    # 上次已完成但本次页数更多时，应该接着往下抓，而不是直接复用
    if cp.get("completed") and settings.resume and not settings.force_refresh:
        next_page = int(cp.get("next_page", 1))
        if next_page > settings.max_pages:
            logging.info(
                f"[{keyword}] 已完成到目标页数，直接复用本次范围内 {len(products)} 条结果"
            )
            return products, environment, True
        cp["completed"] = False

    seen_ids = {str(x.get("item_id")) for x in products if x.get("item_id")}
    paging_state = cp["paging_state"]
    start_page = int(cp.get("next_page", 1))
    empty_pages = 0

    if start_page > settings.max_pages:
        cp["completed"] = True
        save_checkpoint(cp, settings)
        return products, environment, True

    stop_reason = STOP_COMPLETED
    last_page_no = start_page

    for page_no in range(start_page, settings.max_pages + 1):
        last_page_no = page_no
        total_page = resolve_total_page(paging_state, settings)
        if page_no > total_page:
            logging.info(f"[{keyword}] 已超过接口 totalPage={total_page}，停止")
            stop_reason = STOP_BEYOND_TOTAL
            break

        try:
            obj, elapsed, environment = call_mtop_search(
                session=session,
                environment=environment,
                keyword=keyword,
                page_no=page_no,
                paging_state=paging_state,
                settings=settings,
            )
        except (RiskLimitError, NetworkTransportError) as e:
            # 不能跳过本页继续下一页，因为淘宝分页依赖上一页 paging_state。
            # 保留 next_page=当前失败页，下次运行从这里续抓。
            cp["products"] = products
            cp["paging_state"] = paging_state
            cp["next_page"] = page_no
            cp["completed"] = False
            cp["last_error"] = str(e)
            save_checkpoint(cp, settings)
            logging.error(f"[{keyword}] {e}")
            logging.warning(
                f"[{keyword}] 本关键词暂时停止，但已成功抓到的 {len(products)} 条"
                f"会继续导出 Excel。下次运行会从第 {page_no} 页继续。"
            )
            return products, environment, False

        if settings.save_raw_response:
            raw_path = settings.raw_dir / f"{safe_filename(keyword)}_page_{page_no}.json"
            atomic_write_json(raw_path, obj)

        page_products, custom_cards, raw_items = extract_products(obj, page_no)
        paging_state = update_paging_state(obj, paging_state)

        new_count = 0
        duplicate_count = 0
        ad_count = 0
        video_count = 0

        for p in page_products:
            if settings.filter_ads and p["is_ad"]:
                continue

            item_id = p["item_id"]
            if item_id in seen_ids:
                duplicate_count += 1
                continue

            seen_ids.add(item_id)
            p["global_rank"] = len(products) + 1
            products.append(p)
            new_count += 1
            ad_count += int(p["is_ad"])
            video_count += int(p["has_video"])

        logging.info(
            f"[{keyword}] 第{page_no}页：itemsArray={len(raw_items)}，"
            f"真实商品={len(page_products)}，非商品卡={len(custom_cards)}，"
            f"新增={new_count}，跨页重复={duplicate_count}，"
            f"视频={video_count}，广告={ad_count}，耗时={elapsed:.2f}s"
        )
        logging.info(
            f"[{keyword}] paging: sourceS={paging_state.get('sourceS')} | "
            f"bcoffset={paging_state.get('bcoffset')} | "
            f"ntoffset={paging_state.get('ntoffset')} | "
            f"totalPage={paging_state.get('totalPage')} | "
            f"totalResults={paging_state.get('totalResults')}"
        )

        if new_count == 0:
            empty_pages += 1
        else:
            empty_pages = 0

        cp["products"] = products
        cp["paging_state"] = paging_state
        cp["next_page"] = page_no + 1
        cp["completed"] = False
        save_checkpoint(cp, settings)

        if empty_pages >= settings.max_empty_pages:
            logging.warning(f"[{keyword}] 连续 {empty_pages} 页无新增商品，提前停止")
            stop_reason = STOP_EMPTY
            break

        total_page = resolve_total_page(paging_state, settings)
        if page_no >= min(settings.max_pages, total_page):
            break

        cooling.page_cooldown(page_no, settings)

    # ------------------------------------------------------------
    # 收尾：next_page 的记法决定下次重跑会不会浪费一次注定失败的请求
    # ------------------------------------------------------------
    if stop_reason == STOP_EMPTY:
        # 本次目标页数内已探底，下次提高页数时直接从新页开始
        final_next = settings.max_pages + 1
    elif stop_reason == STOP_BEYOND_TOTAL:
        # 停在第 totalPage+1 页。下次从 totalPage+1 开始，
        # 循环开头的 totalPage 检查会立刻命中，不会真的发请求。
        final_next = min(last_page_no, settings.max_pages + 1)
    else:
        final_next = min(settings.max_pages + 1, int(cp.get("next_page", 1)))

    cp["products"] = products
    cp["paging_state"] = paging_state
    cp["next_page"] = final_next
    cp["completed"] = True
    cp.pop("last_error", None)
    save_checkpoint(cp, settings)

    logging.info(f"[{keyword}] 完成，共 {len(products)} 条去重商品")
    return products, environment, True
