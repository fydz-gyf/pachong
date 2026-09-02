"""把 MTop 响应解析成商品记录。"""

from __future__ import annotations

from .utils import clean_title, is_truthy, normalize_url


def extract_products(obj: dict, page_no: int) -> tuple[list[dict], list[dict], list]:
    """返回 (真实商品, 非商品卡, 原始 items)。"""
    data = obj.get("data") or {}
    items = data.get("itemsArray") or []

    products: list[dict] = []
    custom_cards: list[dict] = []

    for raw_position, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue

        item_id = str(item.get("item_id") or "").strip()
        if not item_id:
            custom_cards.append(
                {
                    "raw_position": raw_position,
                    "customCardType": item.get("customCardType", ""),
                }
            )
            continue

        price_show = item.get("priceShow") or {}
        display_price = price_show.get("price") or item.get("price") or ""
        original_price = item.get("price") or ""

        shop_info = item.get("shopInfo") or {}
        shop_name = shop_info.get("title") or item.get("nick") or ""

        image_url = normalize_url(item.get("pic_path") or "")

        video = item.get("video") or {}
        if not isinstance(video, dict):
            video = {}
        video_url = normalize_url(video.get("videoUrl") or "")
        video_cover = normalize_url(video.get("coverUrl") or "")

        products.append(
            {
                "page": page_no,
                "raw_position": raw_position,
                "page_rank": len(products) + 1,
                "item_id": item_id,
                "title": clean_title(item.get("title", "")),
                "price": display_price,
                "original_price": original_price,
                "price_desc": price_show.get("priceDesc", ""),
                "sales": item.get("realSales", ""),
                "shop_name": shop_name,
                "seller_nick": item.get("nick", ""),
                "location": item.get("procity", ""),
                "image_url": image_url,
                "product_url": f"https://item.taobao.com/item.htm?id={item_id}",
                "is_ad": is_truthy(item.get("isP4p", False)),
                "has_video": bool(video_url),
                "video_url": video_url,
                "video_cover": video_cover,
                "category": str(
                    item.get("leafCategory")
                    or item.get("category")
                    or ""
                ),
            }
        )

    return products, custom_cards, items
