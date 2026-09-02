from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any

from ..utils import first_nonempty, get_path, normalize_bool, normalize_walmart_url, to_number

NEXT_DATA_RE = re.compile(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.I | re.S)


def parse_next_data(html: str) -> dict | None:
    match = NEXT_DATA_RE.search(html or "")
    if not match:
        return None
    text = match.group(1).strip()
    for candidate in (text, html_lib.unescape(text)):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, str):
                obj = json.loads(obj)
            return obj if isinstance(obj, dict) else None
        except Exception:
            pass
    return None


def find_search_result(next_data: dict | None) -> dict | None:
    if not isinstance(next_data, dict):
        return None

    # Current Walmart desktop search structure (fast path).
    direct = get_path(next_data, "props", "pageProps", "initialData", "searchResult")
    if isinstance(direct, dict):
        return direct

    # Structure-tolerant fallback.
    queue = [next_data]
    seen = set()
    visited = 0
    while queue and visited < 30000:
        node = queue.pop(0)
        visited += 1
        if not isinstance(node, (dict, list)):
            continue
        ident = id(node)
        if ident in seen:
            continue
        seen.add(ident)
        if isinstance(node, dict):
            sr = node.get("searchResult")
            if isinstance(sr, dict):
                return sr
            queue.extend(node.values())
        else:
            queue.extend(node)
    return None


def find_item_stacks(next_data: dict) -> list:
    sr = find_search_result(next_data)
    if isinstance(sr, dict):
        stacks = sr.get("itemStacks")
        if isinstance(stacks, list):
            return stacks

    queue = [next_data]
    seen = set()
    visited = 0
    while queue and visited < 30000:
        node = queue.pop(0)
        visited += 1
        if not isinstance(node, (dict, list)):
            continue
        ident = id(node)
        if ident in seen:
            continue
        seen.add(ident)
        if isinstance(node, dict):
            stacks = node.get("itemStacks")
            if isinstance(stacks, list) and any(
                isinstance(s, dict) and isinstance(s.get("items"), list) for s in stacks
            ):
                return stacks
            queue.extend(node.values())
        else:
            queue.extend(node)
    return []


def _flatten(stacks: list):
    rows = []
    raw_position = 0
    for stack_index, stack in enumerate(stacks, start=1):
        for item in stack.get("items", []) if isinstance(stack, dict) else []:
            raw_position += 1
            if isinstance(item, dict):
                rows.append((stack_index, raw_position, item))
    return rows


def _nonzero_number(value: Any):
    if value in (None, "", {}):
        return None
    n = to_number(value)
    if n in (None, "", 0, 0.0):
        return None
    return n


def _price_line_value(pi: dict, line_types: tuple[str, ...], keys: tuple[str, ...]):
    details = pi.get("priceDetails") if isinstance(pi.get("priceDetails"), dict) else {}
    lines = details.get("priceLines") if isinstance(details.get("priceLines"), list) else []
    wanted_line_types = {x.upper() for x in line_types}
    wanted_keys = {x.upper() for x in keys}
    for line in lines:
        if not isinstance(line, dict):
            continue
        if str(line.get("lineType") or "").upper() not in wanted_line_types:
            continue
        values = line.get("values") if isinstance(line.get("values"), list) else []
        for row in values:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "").upper()
            if key in wanted_keys:
                n = _nonzero_number(row.get("value"))
                if n is not None:
                    return n
    return None


def _price(item: dict, current=True):
    pi = item.get("priceInfo") if isinstance(item.get("priceInfo"), dict) else {}

    # Walmart's current desktop HTML (2026-09) stores real prices in
    # priceInfo.priceDetails.priceLines while top-level item.price is often 0.
    if current:
        n = _price_line_value(
            pi,
            ("DISCOUNTED_PRICE", "CURRENT_PRICE", "BASE_PRICE", "FINAL_PRICE"),
            ("PRICE", "CURRENT_PRICE"),
        )
        if n is not None:
            return n
    else:
        n = _price_line_value(
            pi,
            ("COMPARISON", "WAS_PRICE", "LIST_PRICE"),
            ("WAS_PRICE", "LIST_PRICE", "PRICE"),
        )
        if n is not None:
            return n

    candidates = [
        get_path(pi, "currentPrice", "price"),
        get_path(pi, "currentPrice", "priceString"),
        get_path(pi, "currentPrice", "displayValue"),
        pi.get("itemPrice"),
        pi.get("linePrice"),
        pi.get("linePriceDisplay"),
        pi.get("minPrice"),
        item.get("price"),
    ] if current else [
        get_path(pi, "wasPrice", "price"),
        get_path(pi, "wasPrice", "priceString"),
        get_path(pi, "listPrice", "price"),
        get_path(pi, "listPrice", "priceString"),
        item.get("wasPrice"),
        item.get("listPrice"),
    ]
    for value in candidates:
        if isinstance(value, dict):
            value = first_nonempty(value.get("price"), value.get("displayValue"), value.get("value"))
        n = _nonzero_number(value)
        if n is not None:
            return n
    return ""


def _image(item: dict) -> str:
    candidates = [
        item.get("image"),
        item.get("imageUrl"),
        item.get("primaryImageUrl"),
        get_path(item, "imageInfo", "thumbnailUrl"),
        get_path(item, "imageInfo", "primaryImage", "url"),
    ]
    all_images = get_path(item, "imageInfo", "allImages", default=[])
    if isinstance(all_images, list) and all_images:
        candidates.append(all_images[0])
    for value in candidates:
        url = normalize_walmart_url(value)
        if url:
            return url
    return ""


def _availability(item: dict) -> str:
    for val in [
        item.get("availabilityStatus"),
        item.get("availabilityStatusV2"),
        get_path(item, "availabilityStatusV2", "value"),
        get_path(item, "availability", "status"),
    ]:
        if isinstance(val, dict):
            val = first_nonempty(val.get("display"), val.get("value"), val.get("status"))
        if val not in (None, "", {}):
            return str(val)
    return ""


def _fulfillment(item: dict) -> str:
    values: list[str] = []
    for key in ("fulfillmentBadge", "fulfillmentTitle", "shippingText", "deliveryDate"):
        if item.get(key):
            values.append(str(item[key]))
    for path in (("fulfillmentSummary", "fulfillment"), ("fulfillmentSummary", "deliveryDate"), ("fulfillmentOptions",)):
        val = get_path(item, *path)
        if val:
            values.append(json.dumps(val, ensure_ascii=False, separators=(",", ":")) if isinstance(val, (dict, list)) else str(val))
    dedup = []
    for value in values:
        value = re.sub(r"\s+", " ", value).strip()
        if value and value not in dedup:
            dedup.append(value)
    return " | ".join(dedup)[:800]


def _sponsored(item: dict) -> bool:
    values = [
        item.get("isSponsored"),
        item.get("isSponsoredFlag"),
        item.get("sponsoredProduct"),
        item.get("sponsored"),
        item.get("adStatus"),
    ]
    return any(normalize_bool(v) for v in values if v not in (None, "")) or bool(
        item.get("sponsoredProductUrl") or item.get("sponsoredProductId")
    )


def parse_search_items(next_data: dict, page_no: int) -> tuple[list[dict], int]:
    rows = _flatten(find_item_stacks(next_data))
    products = []
    page_rank = 0
    for stack_index, raw_position, item in rows:
        # Ignore placeholders/modules that happen to carry an id/name.
        typename = str(item.get("__typename") or "")
        if typename and typename != "Product":
            continue

        item_id = first_nonempty(item.get("usItemId"), item.get("id"), item.get("productId"))
        title = first_nonempty(item.get("name"), item.get("title"))
        if not item_id or not title:
            continue
        page_rank += 1
        rating = first_nonempty(
            item.get("averageRating"),
            get_path(item, "rating", "averageRating"),
            get_path(item, "rating", "rating"),
        )
        reviews = first_nonempty(
            item.get("numberOfReviews"),
            item.get("reviewCount"),
            get_path(item, "rating", "numberOfReviews"),
            get_path(item, "rating", "count"),
        )
        products.append(
            {
                "item_id": str(item_id),
                "title": str(title),
                "page": page_no,
                "page_rank": page_rank,
                "raw_position": raw_position,
                "stack_index": stack_index,
                "price": _price(item, True),
                "original_price": _price(item, False),
                "rating": to_number(rating),
                "review_count": to_number(reviews),
                "brand": str(
                    first_nonempty(
                        item.get("brand"),
                        item.get("brandName"),
                        get_path(item, "manufacturer", "brand"),
                    )
                    or ""
                ),
                "seller": str(
                    first_nonempty(
                        item.get("sellerName"),
                        item.get("sellerDisplayName"),
                        get_path(item, "seller", "name"),
                    )
                    or ""
                ),
                "availability": _availability(item),
                "fulfillment": _fulfillment(item),
                "is_sponsored": _sponsored(item),
                "product_url": normalize_walmart_url(
                    first_nonempty(
                        item.get("canonicalUrl"),
                        item.get("productUrl"),
                        item.get("productPageUrl"),
                    )
                ),
                "image_url": _image(item),
                "raw_type": str(first_nonempty(item.get("type"), item.get("classType"), "")),
            }
        )
    return products, len(rows)


def is_block_page(html: str, title: str = "") -> bool:
    """Identify an actual Walmart verification page without false positives.

    Normal Walmart search pages load PerimeterX/reCAPTCHA-related libraries and include
    those hostnames in CSP. Therefore generic strings such as "perimeterx" or "captcha"
    are NOT sufficient evidence that the current page is a challenge.
    """
    page_title = re.sub(r"\s+", " ", (title or "")).strip().lower()
    if any(marker in page_title for marker in ("robot or human", "verify you are human", "access denied")):
        return True

    head = (html or "")[:40000].lower()
    strong_markers = (
        "<title>robot or human?</title>",
        "<title>robot or human</title>",
        "px-captcha",
        "data-testid=\"px-captcha\"",
        "data-testid='px-captcha'",
        "press &amp; hold",
        "press & hold",
        "verify you are human to continue",
        "please verify that you are a human",
    )
    return any(marker in head for marker in strong_markers)


def looks_like_no_results(next_data: dict | None, html: str) -> bool:
    low = (html or "").lower()
    if any(
        p in low
        for p in (
            "we couldn’t find any matches",
            "we couldn't find any matches",
            "no results for",
        )
    ):
        return True

    sr = find_search_result(next_data)
    if not isinstance(sr, dict):
        return False

    # Only call this a no-results page when Walmart itself reports a zero count.
    for key in ("aggregatedCount", "count", "gridItemsCount"):
        value = sr.get(key)
        try:
            if value is not None and int(value) == 0:
                return True
        except Exception:
            pass
    return False
