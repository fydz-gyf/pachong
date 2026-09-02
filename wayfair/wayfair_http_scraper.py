from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import random
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


DEFAULT_URL = "https://www.wayfair.com/furniture/sb0/accent-chairs-c416225.html"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)
BLOCK_MARKERS = (
    "px-captcha",
    "press & hold",
    "access to this page has been denied",
)

REVIEWS_GRAPHQL_ENDPOINT = "https://www.wayfair.com/federation/graphql"
REVIEWS_GRAPHQL_QUERY = """
query ReviewsListPossibleMPLDataQuery(
  $sku: String!
  $selections: [MarketplaceListingVariantSelectionObfuscatedInput!]!
  $firstReview: Int!
  $afterReview: String
  $sort: ReviewSortIdInput
  $filter: ReviewFilter
  $includeImages: Boolean!
) {
  listingVariant: possibleMarketplaceListingVariantByDisplaySku(
    sku: $sku
    selections: $selections
  ) {
    ... on MarketplaceListingVariant {
      id
      ...ReviewsList_MarketplaceListingVariant_Fragment
    }
  }
}

fragment ReviewsList_MarketplaceListingVariant_Fragment on MarketplaceListingVariant {
  id
  reviewslist: listing {
    id
    reviews(
      first: $firstReview
      after: $afterReview
      sort: $sort
      filter: $filter
    ) {
      pageInfo {
        endCursor
      }
      edges {
        node {
          ...Review_MarketplaceListingVariant_Fragment
        }
      }
      totalCount
    }
  }
}

fragment Review_MarketplaceListingVariant_Fragment on MarketplaceListingReview {
  reviewId
  badge
  badgeDescription
  body
  choices {
    name
    value
  }
  formattedDate
  locale
  reviewerGivenName
  reviewerLocation
  rating
  isTranslatable
  isTranslated
  ...ReviewImages_MarketplaceListingVariant_Fragment
    @include(if: $includeImages)
}

fragment ReviewImages_MarketplaceListingVariant_Fragment on MarketplaceListingReview {
  images {
    imageId
  }
}
"""

FIELDS = [
    "page",
    "pageRank",
    "globalRank",
    "leadImage",
    "name",
    "sku",
    "brand",
    "storeName",
    "storeNameStatus",
    "selectedChoice",
    "variantCountText",
    "price",
    "previousPrice",
    "unitPrice",
    "unitPriceSuffix",
    "currency",
    "discountPercent",
    "rating",
    "reviewCount",
    "flag",
    "speedBadge",
    "estimatedArrival",
    "inventoryStatus",
    "isBestValue",
    "isSponsored",
    "variantUrl",
    "url",
    "sourceUrl",
    "scrapedAt",
]

HEADERS_ZH = [
    "页码",
    "页内排名",
    "全局展示排名",
    "主图URL",
    "商品标题",
    "SKU",
    "品牌名",
    "店铺名/卖家名",
    "店铺字段状态",
    "当前展示变体",
    "变体数量",
    "当前价格",
    "原价",
    "单件价格",
    "单件价格说明",
    "币种",
    "折扣(%)",
    "星级",
    "评论数",
    "活动标签",
    "配送速度",
    "预计到达",
    "库存状态",
    "是否Best Value",
    "是否Sponsored",
    "带变体参数URL",
    "规范商品URL",
    "来源类目页",
    "采集时间",
]

REVIEW_FIELDS = [
    "sku",
    "productName",
    "brand",
    "productUrl",
    "reviewId",
    "rating",
    "reviewBody",
    "reviewerName",
    "reviewerLocation",
    "reviewDate",
    "badge",
    "badgeDescription",
    "choices",
    "reviewImageIds",
    "locale",
    "isTranslatable",
    "isTranslated",
    "scrapedAt",
]

REVIEW_HEADERS_ZH = [
    "SKU",
    "商品标题",
    "品牌",
    "商品URL",
    "评论ID",
    "评分",
    "评论内容",
    "评论者",
    "评论地点",
    "评论日期",
    "徽章",
    "徽章说明",
    "商品选项",
    "评论图片ID",
    "语言",
    "可翻译",
    "已翻译",
    "抓取时间",
]

REVIEW_STATUS_FIELDS = [
    "sku",
    "productName",
    "productUrl",
    "status",
    "scrapedReviewCount",
    "totalReviewCount",
    "scrapedAt",
    "error",
]

REVIEW_STATUS_HEADERS_ZH = [
    "SKU",
    "商品标题",
    "商品URL",
    "状态",
    "本次抓取评论数",
    "商品评论总数",
    "抓取时间",
    "错误",
]

REVIEW_SUMMARY_FIELDS = [
    "sku",
    "productName",
    "productUrl",
    "status",
    "scrapedReviewCount",
    "totalReviewCount",
    "averageRating",
    "rating5Count",
    "rating4Count",
    "rating3Count",
    "rating2Count",
    "rating1Count",
    "scrapedAt",
    "error",
]

REVIEW_SUMMARY_HEADERS_ZH = [
    "SKU",
    "商品名称",
    "商品URL",
    "抓取状态",
    "已抓取评论数",
    "评论总数",
    "平均评分",
    "5星评论数",
    "4星评论数",
    "3星评论数",
    "2星评论数",
    "1星评论数",
    "抓取时间",
    "错误信息",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wayfair listing scraper without a browser")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--end-page", type=int, default=50)
    parser.add_argument("--output-dir", default="output_http")
    parser.add_argument("--delay-min", type=float, default=3.0)
    parser.add_argument("--delay-max", type=float, default=6.0)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--export-every", type=int, default=5)
    parser.add_argument("--keep-html", action="store_true")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument(
        "--offline-html-dir",
        default="",
        help="Parse local wayfair_pageN.html files instead of requesting the website",
    )
    parser.add_argument(
        "--offline-product-html-dir",
        default="",
        help="Parse local <SKU>.html product pages for review scraping.",
    )
    parser.add_argument(
        "--scrape-reviews",
        action="store_true",
        help="Open the product review data flow and fetch all reviews with cursor pagination.",
    )
    parser.add_argument(
        "--reviews-only",
        action="store_true",
        help="Skip category listing pages and scrape reviews from an existing product list.",
    )
    parser.add_argument(
        "--reviews-per-product",
        type=int,
        default=0,
        help="Maximum reviews per product; 0 means fetch all reviews (default: 0).",
    )
    parser.add_argument(
        "--review-page-size",
        type=int,
        default=10,
        help="Number of reviews requested per review page (default: 10).",
    )
    parser.add_argument(
        "--review-workers",
        type=int,
        default=1,
        help="Concurrent product-detail requests for review scraping.",
    )
    parser.add_argument(
        "--review-timeout",
        type=int,
        default=120,
        help="Timeout in seconds for each product-detail request.",
    )
    parser.add_argument(
        "--review-retries",
        type=int,
        default=3,
        help="Number of attempts for each product-detail request.",
    )
    parser.add_argument(
        "--review-browser",
        action="store_true",
        help="Use a normal Chrome session for review pagination when HTTP requests are blocked.",
    )
    parser.add_argument(
        "--review-headless",
        action="store_true",
        help="Run the Chrome review fallback headlessly.",
    )
    parser.add_argument(
        "--cookie-header-file",
        default="",
        help="Optional plain Cookie header file for the browser review fallback.",
    )
    parser.add_argument(
        "--keyword",
        default="",
        help="Free-text keyword search mode, e.g. 'wicker basket'. Products are "
             "grouped by the 'Narrow Your Search' sub-categories, one worksheet each.",
    )
    parser.add_argument(
        "--category-keywords",
        default="",
        help="Comma-separated refined keywords to use as categories. When omitted, "
             "sub-categories are auto-discovered from the keyword search page.",
    )
    parser.add_argument(
        "--category-pages",
        type=int,
        default=1,
        help="Number of listing pages to scrape per category in classification modes (default: 1).",
    )
    parser.add_argument(
        "--categorize",
        action="store_true",
        help="URL category mode: group products by the 'Narrow Your Search' "
             "sub-categories and write one worksheet per category.",
    )
    return parser.parse_args()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_page_url(base_url: str, page_number: int) -> str:
    parts = urlsplit(base_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if page_number <= 1:
        query.pop("curpage", None)
    else:
        query["curpage"] = str(page_number)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def canonical_url(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int | None:
    number = to_float(value)
    return int(number) if number is not None else None


def bool_value(value: Any) -> bool:
    return bool(value is True or str(value).lower() == "true")


def is_challenge_html(html: str) -> bool:
    lowered = html.lower()
    return any(marker in lowered for marker in BLOCK_MARKERS)


def highest_srcset_url(srcset: str | None) -> str | None:
    if not srcset:
        return None
    candidates: list[tuple[int, str]] = []
    for item in srcset.split(","):
        parts = item.strip().split()
        if not parts:
            continue
        url = parts[0]
        width = 0
        if len(parts) > 1 and parts[1].endswith("w"):
            try:
                width = int(parts[1][:-1])
            except ValueError:
                width = 0
        candidates.append((width, url))
    return max(candidates, default=(0, None))[1]


def price_by_variation(metadata: dict[str, Any], variation: str) -> tuple[float | None, str | None]:
    variation = variation.upper()
    for prefix in ("first", "second", "third"):
        display = str(metadata.get(f"{prefix}PriceDisplayVariation") or "").upper()
        if display == variation:
            return to_float(metadata.get(f"{prefix}PriceValue")), metadata.get(f"{prefix}PriceSuffix")
    return None, None


def parse_listing_page(html: str, page_number: int, source_url: str) -> list[dict[str, Any]]:
    if is_challenge_html(html):
        raise RuntimeError("Wayfair returned a PerimeterX challenge page")

    soup = BeautifulSoup(html, "lxml")
    rows: list[dict[str, Any]] = []
    page_seen: set[str] = set()

    selector = '[data-test-id="CardWrapper"][data-tracking-metadata]'
    for card in soup.select(selector):
        raw_metadata = card.get("data-tracking-metadata")
        if not raw_metadata:
            continue

        try:
            wrapper_data = json.loads(raw_metadata)
        except json.JSONDecodeError:
            continue

        metadata = wrapper_data.get("metadata") or {}
        sku = metadata.get("displayListingId")
        title = metadata.get("listingCardName")
        brand = metadata.get("manufacturerName")

        # These three values distinguish the 48 real listing cards from
        # navigation placeholders, banners, and duplicated shell elements.
        if not sku or not title or not brand:
            continue
        if sku in page_seen:
            continue

        link = card.select_one('a[aria-label][href*="/pdp/"]')
        if link is None:
            link = card.select_one('a[href*="/pdp/"]')
        variant_url = link.get("href") if link else None
        if not variant_url:
            continue

        if variant_url.startswith("/"):
            variant_url = "https://www.wayfair.com" + variant_url
        product_url = canonical_url(variant_url)

        image = card.select_one('img[data-name-id="ListingCardImageCarouselLeadImage"]')
        if image is None:
            image = card.select_one('img[src*="wfcdn.com"], img[srcset*="wfcdn.com"]')
        lead_image = None
        if image is not None:
            lead_image = highest_srcset_url(image.get("srcset")) or image.get("src")

        selected_node = card.select_one('[data-name-id="ListingCardSelectedChoices"]')
        selected_choice = selected_node.get_text(" ", strip=True) if selected_node else None

        # Wayfair listing cards expose a manufacturer/brand, but normally do
        # not expose an Amazon-style third-party seller/store field.
        text = card.get_text(" ", strip=True)
        store_match = re.search(r"\bSold by\s+(.+?)(?=\s{2,}|$)", text, re.I)
        store_name = store_match.group(1).strip() if store_match else None

        price, _ = price_by_variation(metadata, "PRIMARY")
        if price is None:
            price = to_float(metadata.get("firstPriceValue"))
        previous_price, _ = price_by_variation(metadata, "PREVIOUS")
        unit_price, unit_price_suffix = price_by_variation(metadata, "SECONDARY")
        discount = to_int(metadata.get("percentOffValue"))
        if discount is None and price is not None and previous_price and previous_price > 0:
            discount = round((1 - price / previous_price) * 100)

        page_index = wrapper_data.get("index")
        page_rank = page_index + 1 if isinstance(page_index, int) else len(rows) + 1

        row = {
            "page": page_number,
            "pageRank": page_rank,
            "globalRank": None,
            "leadImage": lead_image,
            "name": title,
            "sku": sku,
            "brand": brand,
            "storeName": store_name,
            "storeNameStatus": "Explicit seller shown" if store_name else "Seller not exposed on listing card",
            "selectedChoice": selected_choice,
            "variantCountText": metadata.get("choicesText"),
            "price": price,
            "previousPrice": previous_price,
            "unitPrice": unit_price,
            "unitPriceSuffix": unit_price_suffix,
            "currency": metadata.get("firstPriceCurrencyCode") or "USD",
            "discountPercent": discount,
            "rating": to_float(metadata.get("averageRating")),
            "reviewCount": to_int(metadata.get("totalReviewCount")),
            "flag": metadata.get("flagText") or metadata.get("firstPriceLabel"),
            "speedBadge": str(metadata.get("shippingBadgeText") or "").strip('"') or None,
            "estimatedArrival": metadata.get("deliveryEstimateText"),
            "inventoryStatus": metadata.get("inventoryStatusMessage") or metadata.get("inventoryStatus"),
            "isBestValue": bool_value(metadata.get("bestValueStamp")),
            "isSponsored": bool_value(metadata.get("isSponsored")),
            "variantUrl": variant_url,
            "url": product_url,
            "sourceUrl": source_url,
            "scrapedAt": now_iso(),
        }
        page_seen.add(sku)
        rows.append(row)

    rows.sort(key=lambda item: item["pageRank"])
    return rows


def iter_next_f_payloads(html: str) -> Iterable[str]:
    """Yield decoded Next.js flight payload strings from a product page."""
    soup = BeautifulSoup(html, "lxml")
    decoder = json.JSONDecoder()

    for script in soup.find_all("script"):
        content = script.string or script.get_text()
        marker = "self.__next_f.push("
        marker_index = content.find(marker)
        if marker_index < 0:
            continue

        payload_start = content.find("[", marker_index + len(marker))
        if payload_start < 0:
            continue

        try:
            payload, _ = decoder.raw_decode(content[payload_start:])
        except json.JSONDecodeError:
            continue

        if isinstance(payload, list) and len(payload) > 1 and isinstance(payload[1], str):
            yield payload[1]


def extract_json_value(text: str, start: int) -> str:
    """Extract one JSON object/array starting at or after ``start``."""
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] not in "[{":
        raise ValueError("JSON value did not start with an object or array")

    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    raise ValueError("JSON value was not closed")


def normalize_review_edges(
    edges: Iterable[dict[str, Any]],
    product: dict[str, Any],
    limit: int = 0,
) -> list[dict[str, Any]]:
    """Normalize review connection edges from either HTML or GraphQL."""
    product_url = product.get("url") or product.get("variantUrl") or ""
    product_name = product.get("name") or ""
    brand = product.get("brand") or ""
    sku = product.get("sku") or ""
    scraped_at = now_iso()
    result: list[dict[str, Any]] = []
    seen_review_ids: set[str] = set()

    for edge in edges:
        node = edge.get("node") if isinstance(edge, dict) else None
        if not isinstance(node, dict) or node.get("reviewId") is None:
            continue

        review_id = str(node["reviewId"])
        if review_id in seen_review_ids:
            continue
        seen_review_ids.add(review_id)

        choices = node.get("choices") or []
        choice_text = " | ".join(
            f"{choice.get('name')}: {choice.get('value')}"
            for choice in choices
            if isinstance(choice, dict) and (choice.get("name") or choice.get("value"))
        )
        images = node.get("images") or []
        image_ids = ",".join(
            str(image.get("imageId"))
            for image in images
            if isinstance(image, dict) and image.get("imageId") is not None
        )

        result.append(
            {
                "sku": sku,
                "productName": product_name,
                "brand": brand,
                "productUrl": product_url,
                "reviewId": node.get("reviewId"),
                "rating": to_float(node.get("rating")),
                "reviewBody": node.get("body") or "",
                "reviewerName": node.get("reviewerGivenName") or "",
                "reviewerLocation": node.get("reviewerLocation") or "",
                "reviewDate": node.get("formattedDate") or "",
                "badge": node.get("badge") or "",
                "badgeDescription": node.get("badgeDescription") or "",
                "choices": choice_text,
                "reviewImageIds": image_ids,
                "locale": node.get("locale") or "",
                "isTranslatable": bool_value(node.get("isTranslatable")),
                "isTranslated": bool_value(node.get("isTranslated")),
                "scrapedAt": scraped_at,
            }
        )
        if limit > 0 and len(result) >= limit:
            break

    return result


def parse_product_reviews(
    html: str,
    product: dict[str, Any],
    limit: int = 0,
) -> tuple[list[dict[str, Any]], int | None]:
    """Parse the initial review page embedded in a Wayfair product page."""
    if is_challenge_html(html):
        raise RuntimeError("Wayfair returned a PerimeterX challenge page")
    if limit < 0:
        raise ValueError("Review limit must be zero or greater")

    review_connection: dict[str, Any] | None = None
    for payload in iter_next_f_payloads(html):
        search_from = 0
        while True:
            key_index = payload.find('"reviewslist"', search_from)
            if key_index < 0:
                break
            value_start = payload.find(":", key_index) + 1
            try:
                listing = json.loads(extract_json_value(payload, value_start))
            except (ValueError, json.JSONDecodeError):
                search_from = key_index + len("reviewslist")
                continue

            reviews = listing.get("reviews") if isinstance(listing, dict) else None
            if isinstance(reviews, dict) and isinstance(reviews.get("edges"), list):
                if review_connection is None or len(reviews["edges"]) > len(review_connection.get("edges", [])):
                    review_connection = reviews
            search_from = key_index + len("reviewslist")

    if review_connection is None:
        return [], None

    result = normalize_review_edges(review_connection.get("edges", []), product, limit)
    total_count = to_int(review_connection.get("totalCount"))
    return result, total_count


def find_curl() -> str:
    curl_path = shutil.which("curl.exe") or shutil.which("curl")
    if not curl_path:
        raise FileNotFoundError("curl.exe was not found on this computer")
    return curl_path


def fetch_with_curl(
    curl_path: str,
    page_url: str,
    output_path: Path,
    cookie_path: Path,
    timeout: int,
) -> tuple[int, str]:
    command = [
        curl_path,
        "-L",
        "--compressed",
        "--silent",
        "--show-error",
        "--connect-timeout",
        "30",
        "--max-time",
        str(timeout),
        "-A",
        USER_AGENT,
        "-H",
        "Accept-Language: en-US,en;q=0.9",
        "-H",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "-b",
        str(cookie_path),
        "-c",
        str(cookie_path),
        "-o",
        str(output_path),
        "-w",
        "%{http_code}",
        page_url,
    ]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    status_text = completed.stdout.strip()[-3:] if completed.stdout else "000"
    try:
        status = int(status_text)
    except ValueError:
        status = 0
    return status, completed.stderr.strip()


def extract_js_string(html: str, variable: str) -> str:
    """Extract a quoted JavaScript assignment from escaped or normal HTML."""
    pattern = rf"{re.escape(variable)}\s*=\s*(?:\\)?[\"']([^\"'\\]+)(?:\\)?[\"']"
    match = re.search(pattern, html)
    return match.group(1) if match else ""


def extract_fetch_data(html: str) -> dict[str, Any]:
    """Read the request context Wayfair embeds for its browser GraphQL client."""
    marker = "window.__FETCH_DATA__ = "
    start = html.find(marker)
    if start < 0:
        return {}
    start += len(marker)
    end = html.find(";", start)
    if end < 0:
        return {}

    raw = html[start:end].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            return json.loads(raw.encode("utf-8").decode("unicode_escape"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}


def read_cookie_header(cookie_path: Path) -> str:
    """Read either a curl Netscape jar or a plain Cookie header file."""
    if cookie_path.is_dir():
        nested_cookie_path = cookie_path / "cookies.txt"
        if nested_cookie_path.exists():
            cookie_path = nested_cookie_path
        else:
            raise IsADirectoryError(
                f"Cookie path is a directory and does not contain cookies.txt: {cookie_path}"
            )
    if not cookie_path.exists():
        return ""
    raw_text = cookie_path.read_text(encoding="utf-8", errors="ignore").strip()
    if raw_text and not raw_text.startswith("# Netscape HTTP Cookie File"):
        if raw_text.lower().startswith("cookie:"):
            raw_text = raw_text.split(":", 1)[1].strip()
        return raw_text

    cookies: list[str] = []
    for line in raw_text.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) >= 7 and "wayfair.com" in fields[0]:
            cookies.append(f"{fields[5]}={fields[6]}")
    return "; ".join(cookies)


def parse_cookie_header(cookie_header: str) -> list[dict[str, str]]:
    """Convert a Cookie header into Playwright browser-context cookies."""
    result: list[dict[str, str]] = []
    for token in cookie_header.split(";"):
        token = token.strip()
        if "=" not in token:
            continue
        name, value = token.split("=", 1)
        name = name.strip()
        if name:
            result.append(
                {
                    "name": name,
                    "value": value.strip(),
                    "domain": ".wayfair.com",
                    "path": "/",
                }
            )
    return result


def build_reviews_graphql_headers(
    html: str,
    product_url: str,
    cookie_path: Path,
) -> dict[str, str]:
    """Build the same request context used by Wayfair's frontend client."""
    fetch_data = extract_fetch_data(html)
    shared_headers = fetch_data.get("sharedHeaders")
    headers = {
        str(key): str(value)
        for key, value in (shared_headers.items() if isinstance(shared_headers, dict) else [])
        if value is not None
    }
    transaction_id = extract_js_string(html, "window.__transactionID__")
    page_view_id = extract_js_string(html, "window.__pageViewID__")
    anchor_page_view_id = extract_js_string(html, "window.__anchorPageViewID__") or page_view_id
    locale = headers.get("wf-locale") or "en-US"

    headers.update(
        {
            "Accept": "application/json",
            "Accept-Language": f"{locale},en;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://www.wayfair.com",
            "Referer": product_url,
            "User-Agent": USER_AGENT,
            "x-wf-way": "true",
            "x-parent-txid": transaction_id,
            "x-wayfair-locale": locale,
            "x-oi-client": "sf-ui-web",
            "x-txid": transaction_id,
            "wf-pageview-id": page_view_id,
            "wf-anchorpageview-id": anchor_page_view_id,
        }
    )
    cookie_header = read_cookie_header(cookie_path)
    if cookie_header:
        headers["Cookie"] = cookie_header
    return headers


def post_reviews_graphql(
    product: dict[str, Any],
    html: str,
    cookie_path: Path,
    after_review: str | None,
    page_size: int,
    timeout: int,
) -> dict[str, Any]:
    """Request one page from Wayfair's review connection."""
    product_url = str(product.get("variantUrl") or product.get("url") or "")
    sku = str(product.get("sku") or "")
    request_body = {
        "operationName": "ReviewsListPossibleMPLDataQuery",
        "variables": {
            "sku": sku,
            "selections": [],
            "firstReview": page_size,
            "afterReview": after_review,
            "sort": "RELEVANCE_DESC",
            "filter": {"ratings": None},
            "includeImages": True,
        },
        "query": REVIEWS_GRAPHQL_QUERY,
    }
    request = Request(
        REVIEWS_GRAPHQL_ENDPOINT,
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers=build_reviews_graphql_headers(html, product_url, cookie_path),
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Review GraphQL HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Review GraphQL request failed: {exc.reason}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Review GraphQL returned invalid JSON: {raw[:300]}") from exc
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if errors:
        messages = "; ".join(
            str(error.get("message") or error)
            for error in errors
            if isinstance(error, dict)
        )
        raise RuntimeError(f"Review GraphQL error: {messages or errors}")
    return payload


def fetch_all_product_reviews(
    product: dict[str, Any],
    html: str,
    cookie_path: Path,
    max_reviews: int,
    page_size: int,
    timeout: int,
    retries: int,
) -> tuple[list[dict[str, Any]], int | None, int, bool]:
    """Fetch every review page until totalCount is reached or a cap is met."""
    _, initial_total = parse_product_reviews(html, product, 0)
    if initial_total == 0:
        return [], 0, 0, True

    review_map: dict[str, dict[str, Any]] = {}
    cursor: str | None = None
    total_count = initial_total
    pages_fetched = 0

    for _ in range(1000):
        payload: dict[str, Any] | None = None
        last_error = ""
        for attempt in range(1, max(1, retries) + 1):
            try:
                payload = post_reviews_graphql(
                    product=product,
                    html=html,
                    cookie_path=cookie_path,
                    after_review=cursor,
                    page_size=page_size,
                    timeout=timeout,
                )
                break
            except Exception as exc:
                last_error = str(exc)
                if attempt < max(1, retries):
                    time.sleep(min(5 * attempt, 20))
        if payload is None:
            raise RuntimeError(last_error or "Review GraphQL request failed")

        listing_variant = (payload.get("data") or {}).get("listingVariant")
        connection = (
            (listing_variant or {}).get("reviewslist", {}).get("reviews")
            if isinstance(listing_variant, dict)
            else None
        )
        if not isinstance(connection, dict):
            raise RuntimeError("Review GraphQL response did not include listingVariant.reviewslist.reviews")

        edges = connection.get("edges")
        if not isinstance(edges, list):
            edges = []
        for row in normalize_review_edges(edges, product, 0):
            review_id = str(row.get("reviewId") or "")
            if review_id:
                review_map[review_id] = row

        pages_fetched += 1
        response_total = to_int(connection.get("totalCount"))
        if response_total is not None:
            total_count = response_total

        page_info = connection.get("pageInfo") or {}
        next_cursor = page_info.get("endCursor") if isinstance(page_info, dict) else None
        reached_cap = max_reviews > 0 and len(review_map) >= max_reviews
        reached_total = total_count is not None and len(review_map) >= total_count
        if reached_cap or reached_total or not edges or not next_cursor or next_cursor == cursor:
            break
        cursor = str(next_cursor)
        time.sleep(0.2)

    ordered_reviews = list(review_map.values())
    if max_reviews > 0:
        ordered_reviews = ordered_reviews[:max_reviews]
    complete = (
        total_count == 0
        or (max_reviews > 0 and len(ordered_reviews) >= max_reviews)
        or (total_count is not None and len(ordered_reviews) >= total_count)
    )
    if total_count is None and not complete:
        complete = bool(ordered_reviews) and not cursor
    return ordered_reviews, total_count, pages_fetched, complete


def scrape_one_product_reviews_browser(
    browser: Any,
    product: dict[str, Any],
    cookie_header: str,
    max_reviews: int,
    page_size: int,
    timeout: int,
    retries: int,
    headless: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch all review pages through a normal Chrome session.

    Wayfair may return a challenge to direct HTTP clients while allowing the
    same user session to load the product page in Chrome. The frontend then
    requests reviews through GraphQL as the user clicks "Show More Reviews".
    The first review request is also used as a browser-origin HTTP template for
    cursor pagination. If that fast path is unavailable, the UI remains the
    fallback; it does not spoof fingerprints or bypass challenges.
    """
    sku = str(product.get("sku") or "")
    product_url = str(product.get("url") or product.get("variantUrl") or "")
    scraped_at = now_iso()
    total_count: int | None = None
    review_nodes: dict[str, dict[str, Any]] = {}
    review_next_cursor: str | None = None
    review_request_template: dict[str, Any] | None = None
    pagination_exhausted = False
    pagination_error = ""

    def status_row(status: str, count: int, total: int | None, error: str = "") -> dict[str, Any]:
        return {
            "sku": sku,
            "productName": product.get("name") or "",
            "productUrl": product_url,
            "status": status,
            "scrapedReviewCount": count,
            "totalReviewCount": total,
            "scrapedAt": scraped_at,
            "error": error,
        }

    if not product_url:
        return [], status_row("failed", 0, None, "Product URL is empty")

    context = None
    page = None
    try:
        context = browser.new_context(locale="en-US")
        if cookie_header:
            cookies = parse_cookie_header(cookie_header)
            if cookies:
                context.add_cookies(cookies)
        page = context.new_page()
        page.set_default_timeout(max(5_000, timeout * 1_000))

        def consume_review_payload(payload: Any) -> None:
            nonlocal total_count, review_next_cursor
            if not isinstance(payload, dict):
                return
            listing_variant = (payload.get("data") or {}).get("listingVariant")
            connection = (
                (listing_variant or {}).get("reviewslist", {}).get("reviews")
                if isinstance(listing_variant, dict)
                else None
            )
            if not isinstance(connection, dict):
                return
            response_total = to_int(connection.get("totalCount"))
            if response_total is not None:
                total_count = response_total
            page_info = connection.get("pageInfo") or {}
            next_cursor = page_info.get("endCursor") if isinstance(page_info, dict) else None
            review_next_cursor = str(next_cursor) if next_cursor else None
            edges = connection.get("edges")
            if not isinstance(edges, list):
                return
            for edge in edges:
                if not isinstance(edge, dict):
                    continue
                node = edge.get("node")
                if not isinstance(node, dict):
                    continue
                review_id = str(node.get("reviewId") or "")
                if review_id:
                    review_nodes[review_id] = node

        def capture_request(request: Any) -> None:
            nonlocal review_request_template
            if review_request_template is not None:
                return
            if "/federation/graphql" not in str(request.url) or request.method != "POST":
                return
            try:
                request_body = json.loads(request.post_data or "")
                if request_body.get("operationName") != "ReviewsListPossibleMPLDataQuery":
                    return
                request_headers = request.all_headers()
                browser_headers = {
                    key: value
                    for key, value in request_headers.items()
                    if key.lower() in {"accept", "accept-language", "content-type"}
                    or key.lower().startswith("x-")
                    or key.lower().startswith("wf-")
                }
                review_request_template = {
                    "body": request_body,
                    "headers": browser_headers,
                }
            except Exception:
                return

        def capture_response(response: Any) -> None:
            if "/federation/graphql" not in str(response.url):
                return
            try:
                payload = response.json()
                consume_review_payload(payload)
            except Exception:
                # Non-review GraphQL operations and aborted responses are expected.
                return

        page.on("request", capture_request)
        page.on("response", capture_response)
        last_error = ""
        for attempt in range(1, max(1, retries) + 1):
            try:
                response = page.goto(
                    str(product.get("variantUrl") or product_url),
                    wait_until="domcontentloaded",
                    timeout=max(5_000, timeout * 1_000),
                )
                response_status = response.status if response is not None else 200
                if response_status >= 400:
                    raise RuntimeError(f"Product page HTTP {response_status}")
                break
            except Exception as exc:
                last_error = str(exc)
                if attempt < max(1, retries):
                    page.wait_for_timeout(min(5_000 * attempt, 20_000))
        else:
            return [], status_row("failed", 0, None, last_error or "Chrome product page request failed")

        def visible_reviews_button() -> Any | None:
            selectors = [
                page.locator('[data-rtl-id="reviewsHeaderButton"]'),
                page.locator("button").filter(has_text=re.compile(r"^\s*[\d,.]+\s+Reviews?\s*$", re.I)),
            ]
            for locator in selectors:
                try:
                    for index in range(locator.count()):
                        candidate = locator.nth(index)
                        if candidate.is_visible():
                            return candidate
                except Exception:
                    continue
            return None

        deadline = time.monotonic() + max(5, timeout)
        reviews_button = None
        while time.monotonic() < deadline and reviews_button is None:
            reviews_button = visible_reviews_button()
            if reviews_button is None:
                page.wait_for_timeout(500)
        if reviews_button is None:
            return [], status_row("failed", 0, None, "Review summary button was not found in Chrome")

        reviews_button.click()

        # The first request is asynchronous and can take several seconds after
        # the drawer is opened, so wait for either review data or an empty total.
        first_page_deadline = time.monotonic() + max(5, timeout)
        while (
            time.monotonic() < first_page_deadline
            and not review_nodes
            and total_count != 0
        ):
            page.wait_for_timeout(500)

        def fetch_next_page_in_browser(after_cursor: str, first_review: int) -> None:
            if not review_request_template:
                raise RuntimeError("The browser review GraphQL request was not captured")
            request_body = json.loads(json.dumps(review_request_template["body"]))
            variables = request_body.setdefault("variables", {})
            variables["afterReview"] = after_cursor
            variables["firstReview"] = first_review
            result = page.evaluate(
                """async ({body, headers}) => {
                    const response = await fetch('/federation/graphql', {
                        method: 'POST',
                        credentials: 'include',
                        headers,
                        body: JSON.stringify(body)
                    });
                    const text = await response.text();
                    let payload = null;
                    try { payload = JSON.parse(text); } catch (_) {}
                    return {status: response.status, payload, text: text.slice(0, 500)};
                }""",
                {"body": request_body, "headers": review_request_template["headers"]},
            )
            if not isinstance(result, dict) or int(result.get("status") or 0) >= 400:
                status = result.get("status") if isinstance(result, dict) else "unknown"
                detail = result.get("text") if isinstance(result, dict) else ""
                raise RuntimeError(f"Browser review GraphQL HTTP {status}: {detail}")
            payload = result.get("payload")
            if not isinstance(payload, dict):
                raise RuntimeError("Browser review GraphQL returned invalid JSON")
            if payload.get("errors"):
                raise RuntimeError(f"Browser review GraphQL error: {payload['errors']}")
            consume_review_payload(payload)

        # Use the browser's own authenticated request context for cursor pages.
        # A larger page size is attempted for speed; if Wayfair rejects it, the
        # normal UI pagination below remains available as a safe fallback.
        if review_request_template and review_next_cursor and total_count not in (None, 0):
            try:
                browser_page_size = max(10, min(page_size if page_size > 10 else 50, 100))
                for _ in range(1000):
                    if max_reviews > 0 and len(review_nodes) >= max_reviews:
                        break
                    if total_count is not None and len(review_nodes) >= total_count:
                        pagination_exhausted = True
                        break
                    if not review_next_cursor:
                        pagination_exhausted = True
                        break
                    previous_count = len(review_nodes)
                    previous_cursor = review_next_cursor
                    fetch_next_page_in_browser(review_next_cursor, browser_page_size)
                    if len(review_nodes) <= previous_count and review_next_cursor == previous_cursor:
                        raise RuntimeError("Browser review GraphQL pagination made no progress")
                else:
                    raise RuntimeError("Browser review GraphQL exceeded 1000 pages")
            except Exception as exc:
                pagination_error = str(exc)

        def visible_show_more_button() -> Any | None:
            selectors = [
                page.get_by_role("button", name="Show More Reviews", exact=True),
                page.locator("button").filter(has_text=re.compile(r"^\s*Show More Reviews\s*$", re.I)),
            ]
            for locator in selectors:
                try:
                    for index in range(locator.count()):
                        candidate = locator.nth(index)
                        if candidate.is_visible() and candidate.is_enabled():
                            return candidate
                except Exception:
                    continue
            return None

        # If browser-origin HTTP completed pagination, no UI clicks are needed.
        if not pagination_exhausted and (
            total_count is None or len(review_nodes) < total_count
        ):
            for _ in range(1000):
                if total_count == 0:
                    pagination_exhausted = True
                    break
                if max_reviews > 0 and len(review_nodes) >= max_reviews:
                    break
                if total_count is not None and len(review_nodes) >= total_count:
                    pagination_exhausted = True
                    break

                show_more = None
                button_deadline = time.monotonic() + min(max(10, timeout), 30)
                while time.monotonic() < button_deadline and show_more is None:
                    show_more = visible_show_more_button()
                    if show_more is None:
                        page.wait_for_timeout(500)
                if show_more is None:
                    pagination_exhausted = True
                    break

                previous_count = len(review_nodes)
                show_more.click()
                progress_deadline = time.monotonic() + min(max(10, timeout), 30)
                while time.monotonic() < progress_deadline and len(review_nodes) <= previous_count:
                    page.wait_for_timeout(500)
                if len(review_nodes) <= previous_count:
                    break

        rows = normalize_review_edges(
            [{"node": node} for node in review_nodes.values()],
            product,
            max_reviews,
        )
        complete = (
            total_count == 0
            or (max_reviews > 0 and len(rows) >= max_reviews)
            or (total_count is not None and len(rows) >= total_count)
            or (total_count is None and pagination_exhausted)
        )
        status = (
            "no_reviews"
            if total_count == 0
            else "ok"
            if complete and rows
            else "partial"
            if rows
            else "no_review_data"
        )
        return rows, status_row(
            status,
            len(rows),
            total_count,
            pagination_error if status != "ok" else "",
        )
    except Exception as exc:
        rows = normalize_review_edges(
            [{"node": node} for node in review_nodes.values()],
            product,
            max_reviews,
        )
        return rows, status_row("failed", len(rows), total_count, str(exc))
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


def scrape_reviews_for_products_browser(
    products: list[dict[str, Any]],
    output_dir: Path,
    cookie_path: Path,
    review_path: Path | None,
    review_status_path: Path | None,
    max_reviews: int,
    page_size: int,
    workers: int,
    timeout: int,
    retries: int,
    headless: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run review scraping sequentially in one normal Chrome instance."""
    del output_dir  # Browser pagination is controlled by the browser session.
    if not products:
        return [], []
    if workers != 1:
        print("Review browser fallback uses one Chrome session; forcing --review-workers 1.")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is required for --review-browser. Run: "
            "python -m pip install -r requirements_http.txt && python -m playwright install chrome"
        ) from exc

    cookie_header = read_cookie_header(cookie_path)
    if not cookie_header:
        raise RuntimeError(
            "Cookie file is empty or contains no usable cookies: "
            f"{cookie_path}. Provide the plain Cookie header exported from Chrome "
            "or a non-empty Netscape cookies.txt file."
        )
    reviews: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=headless)
        except Exception as exc:
            raise RuntimeError(
                "Could not launch Chrome for --review-browser. "
                "Install Google Chrome and run: python -m playwright install chrome"
            ) from exc
        try:
            for completed, product in enumerate(products, start=1):
                try:
                    product_reviews, status = scrape_one_product_reviews_browser(
                        browser=browser,
                        product=product,
                        cookie_header=cookie_header,
                        max_reviews=max_reviews,
                        page_size=page_size,
                        timeout=timeout,
                        retries=retries,
                        headless=headless,
                    )
                except Exception as exc:
                    status = {
                        "sku": product.get("sku") or "",
                        "productName": product.get("name") or "",
                        "productUrl": product.get("url") or product.get("variantUrl") or "",
                        "status": "failed",
                        "scrapedReviewCount": 0,
                        "totalReviewCount": None,
                        "scrapedAt": now_iso(),
                        "error": str(exc),
                    }
                    product_reviews = []

                reviews.extend(product_reviews)
                statuses.append(status)
                if review_status_path:
                    append_jsonl(review_status_path, [status])
                if review_path and product_reviews:
                    append_jsonl(review_path, product_reviews)
                print(f"Reviews scraped: {completed}/{len(products)}")
        finally:
            browser.close()
    return reviews, statuses


def scrape_one_product_reviews(
    product: dict[str, Any],
    curl_path: str | None,
    cookie_path: Path,
    temp_dir: Path,
    max_reviews: int,
    page_size: int,
    timeout: int,
    retries: int,
    offline_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch and parse one product page, returning reviews and an audit row."""
    sku = str(product.get("sku") or "")
    product_url = product.get("url") or product.get("variantUrl") or ""
    scraped_at = now_iso()

    def status_row(status: str, count: int, total: int | None, error: str = "") -> dict[str, Any]:
        return {
            "sku": sku,
            "productName": product.get("name") or "",
            "productUrl": product_url,
            "status": status,
            "scrapedReviewCount": count,
            "totalReviewCount": total,
            "scrapedAt": scraped_at,
            "error": error,
        }

    if not product_url:
        return [], status_row("failed", 0, None, "Product URL is empty")

    if offline_dir:
        candidates = [
            offline_dir / f"{sku}.html",
            offline_dir / f"product_{sku}.html",
            offline_dir / f"wayfair_product_{sku}.html",
        ]
        source_path = next((path for path in candidates if path.exists()), None)
        if source_path is None:
            return [], status_row("failed", 0, None, f"Offline product HTML not found for SKU {sku}")
        try:
            html = source_path.read_text(encoding="utf-8", errors="ignore")
            reviews, total = parse_product_reviews(html, product, max_reviews)
            complete = (
                total is None
                or total == 0
                or (max_reviews > 0 and len(reviews) >= max_reviews)
                or (total is not None and len(reviews) >= total)
            )
            status = (
                "no_reviews"
                if total == 0
                else "ok"
                if complete and reviews
                else "partial"
                if reviews
                else "no_review_data"
            )
            return reviews, status_row(status, len(reviews), total)
        except Exception as exc:
            return [], status_row("failed", 0, None, str(exc))

    if not curl_path:
        return [], status_row("failed", 0, None, "curl.exe was not found")

    temp_path = temp_dir / f"product_{hashlib.sha1(sku.encode('utf-8')).hexdigest()}.html"
    last_error = ""
    try:
        for attempt in range(1, max(1, retries) + 1):
            temp_path.unlink(missing_ok=True)
            status, stderr = fetch_with_curl(
                curl_path=curl_path,
                page_url=str(product.get("variantUrl") or product_url),
                output_path=temp_path,
                cookie_path=cookie_path,
                timeout=timeout,
            )
            last_error = f"HTTP {status}" + (f"; {stderr}" if stderr else "")
            if status == 200 and temp_path.exists():
                html = temp_path.read_text(encoding="utf-8", errors="ignore")
                if not is_challenge_html(html):
                    try:
                        initial_reviews, initial_total = parse_product_reviews(html, product, 0)
                        reviews, total, _, complete = fetch_all_product_reviews(
                            product=product,
                            html=html,
                            cookie_path=cookie_path,
                            max_reviews=max_reviews,
                            page_size=page_size,
                            timeout=timeout,
                            retries=retries,
                        )
                        status = (
                            "no_reviews"
                            if total == 0
                            else "ok"
                            if complete and reviews
                            else "partial"
                            if reviews
                            else "no_review_data"
                        )
                        return reviews, status_row(
                            status,
                            len(reviews),
                            total,
                        )
                    except Exception as exc:
                        last_error = str(exc)
                        try:
                            initial_reviews, initial_total = parse_product_reviews(html, product, max_reviews)
                        except Exception:
                            initial_reviews, initial_total = [], None
                        return initial_reviews, status_row(
                            "failed",
                            len(initial_reviews),
                            initial_total,
                            last_error,
                        )

            if attempt < max(1, retries):
                time.sleep(min(20 * attempt, 60))

        return [], status_row("failed", 0, None, last_error or "Product page request failed")
    finally:
        temp_path.unlink(missing_ok=True)


def append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    result: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                result.append(json.loads(line))
    return result


def scrape_reviews_for_products(
    products: list[dict[str, Any]],
    output_dir: Path,
    cookie_path: Path,
    review_path: Path | None,
    review_status_path: Path | None,
    max_reviews: int,
    page_size: int,
    workers: int,
    timeout: int,
    retries: int,
    offline_dir: Path | None = None,
    browser_mode: bool = False,
    browser_headless: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Scrape cursor-paginated reviews for unique products with isolated cookie jars."""
    if not products:
        return [], []
    if browser_mode:
        return scrape_reviews_for_products_browser(
            products=products,
            output_dir=output_dir,
            cookie_path=cookie_path,
            review_path=review_path,
            review_status_path=review_status_path,
            max_reviews=max_reviews,
            page_size=page_size,
            workers=workers,
            timeout=timeout,
            retries=retries,
            headless=browser_headless,
        )

    temp_dir = output_dir / ".review_scrape_tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    worker_count = max(1, workers)
    if offline_dir:
        worker_count = 1

    cookie_paths: list[Path] = []
    for worker_index in range(worker_count):
        worker_cookie = temp_dir / f"cookies_{worker_index:03d}.txt"
        if cookie_path.exists():
            shutil.copy2(cookie_path, worker_cookie)
        cookie_paths.append(worker_cookie)

    curl_path = None if offline_dir else find_curl()
    reviews: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                scrape_one_product_reviews,
                product,
                curl_path,
                cookie_paths[index % worker_count],
                temp_dir,
                max_reviews,
                page_size,
                timeout,
                retries,
                offline_dir,
            ): product
            for index, product in enumerate(products)
        }

        for completed, future in enumerate(as_completed(futures), start=1):
            product = futures[future]
            try:
                product_reviews, status = future.result()
            except Exception as exc:
                status = {
                    "sku": product.get("sku") or "",
                    "productName": product.get("name") or "",
                    "productUrl": product.get("url") or product.get("variantUrl") or "",
                    "status": "failed",
                    "scrapedReviewCount": 0,
                    "totalReviewCount": None,
                    "scrapedAt": now_iso(),
                    "error": str(exc),
                }
                product_reviews = []

            reviews.extend(product_reviews)
            statuses.append(status)
            if review_status_path:
                append_jsonl(review_status_path, [status])
            if review_path and product_reviews:
                append_jsonl(review_path, product_reviews)
            if completed % 10 == 0 or completed == len(products):
                print(f"Reviews scraped: {completed}/{len(products)}")

    return reviews, statuses


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in FIELDS})


def write_review_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in REVIEW_FIELDS})


def write_review_status_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_STATUS_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in REVIEW_STATUS_FIELDS})


def style_review_worksheet(sheet, row_count: int) -> None:
    fill = PatternFill("solid", fgColor="1F4E78")
    for cell in sheet[1]:
        cell.fill = fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions

    widths = [
        16, 42, 22, 55, 14, 9, 80, 18, 22, 14, 18, 48, 34, 20, 10, 12, 12, 26,
    ]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width

    for row in range(2, row_count + 2):
        for col in (2, 4, 7, 8, 9, 11, 12, 13):
            sheet.cell(row, col).alignment = Alignment(vertical="top", wrap_text=True)
        sheet.cell(row, 6).number_format = "0.0"
        for col in (4,):
            value = sheet.cell(row, col).value
            if isinstance(value, str) and value.startswith("http"):
                sheet.cell(row, col).hyperlink = value
                sheet.cell(row, col).style = "Hyperlink"


def style_review_status_worksheet(sheet, row_count: int) -> None:
    fill = PatternFill("solid", fgColor="1F4E78")
    for cell in sheet[1]:
        cell.fill = fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for index, width in enumerate((16, 42, 55, 16, 16, 16, 26, 55), start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    for row in range(2, row_count + 2):
        for col in (2, 3, 8):
            sheet.cell(row, col).alignment = Alignment(vertical="top", wrap_text=True)
        value = sheet.cell(row, 3).value
        if isinstance(value, str) and value.startswith("http"):
            sheet.cell(row, 3).hyperlink = value
            sheet.cell(row, 3).style = "Hyperlink"


def build_review_summary(
    reviews: list[dict[str, Any]],
    review_statuses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate review counts and ratings by SKU for the review workbook."""
    grouped: dict[str, dict[str, Any]] = {}
    for review in reviews:
        sku = str(review.get("sku") or "")
        if not sku:
            continue
        aggregate = grouped.setdefault(
            sku,
            {
                "reviewCount": 0,
                "ratings": [],
                "rating5Count": 0,
                "rating4Count": 0,
                "rating3Count": 0,
                "rating2Count": 0,
                "rating1Count": 0,
            },
        )
        aggregate["reviewCount"] += 1
        rating = to_float(review.get("rating"))
        if rating is None:
            continue
        aggregate["ratings"].append(rating)
        rounded_rating = int(round(rating))
        if 1 <= rounded_rating <= 5:
            aggregate[f"rating{rounded_rating}Count"] += 1

    status_by_sku: dict[str, dict[str, Any]] = {}
    for status in review_statuses:
        sku = str(status.get("sku") or "")
        if sku:
            status_by_sku[sku] = status

    rows: list[dict[str, Any]] = []
    for sku, status in status_by_sku.items():
        aggregate = grouped.get(sku, {})
        ratings = aggregate.get("ratings") or []
        rows.append(
            {
                "sku": sku,
                "productName": status.get("productName") or "",
                "productUrl": status.get("productUrl") or "",
                "status": status.get("status") or "",
                "scrapedReviewCount": status.get("scrapedReviewCount") or 0,
                "totalReviewCount": status.get("totalReviewCount"),
                "averageRating": round(sum(ratings) / len(ratings), 2) if ratings else None,
                "rating5Count": aggregate.get("rating5Count", 0),
                "rating4Count": aggregate.get("rating4Count", 0),
                "rating3Count": aggregate.get("rating3Count", 0),
                "rating2Count": aggregate.get("rating2Count", 0),
                "rating1Count": aggregate.get("rating1Count", 0),
                "scrapedAt": status.get("scrapedAt") or "",
                "error": status.get("error") or "",
            }
        )

    # Keep reviews without a status row visible instead of silently dropping them.
    for sku, aggregate in grouped.items():
        if sku in status_by_sku:
            continue
        ratings = aggregate.get("ratings") or []
        rows.append(
            {
                "sku": sku,
                "productName": "",
                "productUrl": "",
                "status": "unknown",
                "scrapedReviewCount": aggregate.get("reviewCount", 0),
                "totalReviewCount": None,
                "averageRating": round(sum(ratings) / len(ratings), 2) if ratings else None,
                "rating5Count": aggregate.get("rating5Count", 0),
                "rating4Count": aggregate.get("rating4Count", 0),
                "rating3Count": aggregate.get("rating3Count", 0),
                "rating2Count": aggregate.get("rating2Count", 0),
                "rating1Count": aggregate.get("rating1Count", 0),
                "scrapedAt": "",
                "error": "No review status row",
            }
        )
    return rows


def style_review_summary_worksheet(sheet, row_count: int) -> None:
    fill = PatternFill("solid", fgColor="1F4E78")
    for cell in sheet[1]:
        cell.fill = fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    widths = (16, 42, 55, 16, 16, 16, 12, 12, 12, 12, 12, 12, 26, 55)
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    for row in range(2, row_count + 2):
        for col in (2, 3, 14):
            sheet.cell(row, col).alignment = Alignment(vertical="top", wrap_text=True)
        sheet.cell(row, 7).number_format = "0.00"
        value = sheet.cell(row, 3).value
        if isinstance(value, str) and value.startswith("http"):
            sheet.cell(row, 3).hyperlink = value
            sheet.cell(row, 3).style = "Hyperlink"


def write_review_xlsx(
    path: Path,
    reviews: list[dict[str, Any]],
    review_statuses: list[dict[str, Any]],
) -> None:
    """Write a standalone workbook containing review summary and detail."""
    workbook = Workbook()

    summary_sheet = workbook.active
    summary_sheet.title = "ReviewSummary"
    summary_rows = build_review_summary(reviews, review_statuses)
    summary_sheet.append(REVIEW_SUMMARY_HEADERS_ZH)
    for row in summary_rows:
        summary_sheet.append([row.get(field) for field in REVIEW_SUMMARY_FIELDS])
    style_review_summary_worksheet(summary_sheet, len(summary_rows))

    review_sheet = workbook.create_sheet("Reviews")
    review_sheet.append(REVIEW_HEADERS_ZH)
    for row in reviews:
        review_sheet.append([row.get(field) for field in REVIEW_FIELDS])
    style_review_worksheet(review_sheet, len(reviews))

    status_sheet = workbook.create_sheet("ReviewStatus")
    status_sheet.append(REVIEW_STATUS_HEADERS_ZH)
    for row in review_statuses:
        status_sheet.append([row.get(field) for field in REVIEW_STATUS_FIELDS])
    style_review_status_worksheet(status_sheet, len(review_statuses))

    workbook.save(path)


def style_worksheet(sheet, row_count: int) -> None:
    fill = PatternFill("solid", fgColor="1F4E78")
    for cell in sheet[1]:
        cell.fill = fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions

    widths = {
        1: 8, 2: 10, 3: 12, 4: 44, 5: 54, 6: 16, 7: 22, 8: 22, 9: 28,
        10: 24, 11: 14, 12: 12, 13: 12, 14: 12, 15: 16, 16: 8, 17: 11,
        18: 9, 19: 11, 20: 18, 21: 20, 22: 20, 23: 22, 24: 14, 25: 14,
        26: 55, 27: 55, 28: 50, 29: 26,
    }
    for index, width in widths.items():
        sheet.column_dimensions[get_column_letter(index)].width = width

    for row in range(2, row_count + 2):
        sheet.cell(row, 5).alignment = Alignment(vertical="top", wrap_text=True)
        sheet.cell(row, 7).alignment = Alignment(vertical="top", wrap_text=True)
        sheet.cell(row, 9).alignment = Alignment(vertical="top", wrap_text=True)
        sheet.cell(row, 26).alignment = Alignment(vertical="top", wrap_text=True)
        sheet.cell(row, 27).alignment = Alignment(vertical="top", wrap_text=True)
        sheet.cell(row, 28).alignment = Alignment(vertical="top", wrap_text=True)
        sheet.cell(row, 12).number_format = '$0.00'
        sheet.cell(row, 13).number_format = '$0.00'
        sheet.cell(row, 14).number_format = '$0.00'
        sheet.cell(row, 18).number_format = '0.0'
        sheet.cell(row, 19).number_format = '0'
        for col in (4, 26, 27, 28):
            value = sheet.cell(row, col).value
            if isinstance(value, str) and value.startswith("http"):
                sheet.cell(row, col).hyperlink = value
                sheet.cell(row, col).style = "Hyperlink"


def export_xlsx(
    path: Path,
    occurrences: list[dict[str, Any]],
    unique_rows: list[dict[str, Any]],
    reviews: list[dict[str, Any]] | None = None,
    review_statuses: list[dict[str, Any]] | None = None,
) -> None:
    workbook = Workbook()
    occurrence_sheet = workbook.active
    occurrence_sheet.title = "ListingOccurrences"
    occurrence_sheet.append(HEADERS_ZH)
    for row in occurrences:
        occurrence_sheet.append([row.get(field) for field in FIELDS])
    style_worksheet(occurrence_sheet, len(occurrences))

    unique_sheet = workbook.create_sheet("UniqueProducts")
    unique_sheet.append(HEADERS_ZH)
    for row in unique_rows:
        unique_sheet.append([row.get(field) for field in FIELDS])
    style_worksheet(unique_sheet, len(unique_rows))

    summary = workbook.create_sheet("Summary")
    summary.append(["Metric", "Value"])
    summary.append(["Listing occurrences", len(occurrences)])
    summary.append(["Unique products by SKU", len(unique_rows)])
    summary.append(["Pages represented", len({row.get('page') for row in occurrences})])
    summary.append(["Sponsored occurrences", sum(bool(row.get('isSponsored')) for row in occurrences)])
    summary.append(["Seller/store field note", "Wayfair listing cards usually expose brand/manufacturer, not an independent seller store."])
    if reviews is not None:
        summary.append(["Reviews scraped", len(reviews)])
        summary.append(["Products with review status", len(review_statuses or [])])
    for cell in summary[1]:
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.font = Font(color="FFFFFF", bold=True)
    summary.column_dimensions["A"].width = 28
    summary.column_dimensions["B"].width = 85
    summary.freeze_panes = "A2"

    if reviews is not None:
        review_sheet = workbook.create_sheet("Reviews")
        review_sheet.append(REVIEW_HEADERS_ZH)
        for row in reviews:
            review_sheet.append([row.get(field) for field in REVIEW_FIELDS])
        style_review_worksheet(review_sheet, len(reviews))

        status_sheet = workbook.create_sheet("ReviewStatus")
        status_sheet.append(REVIEW_STATUS_HEADERS_ZH)
        for row in review_statuses or []:
            status_sheet.append([row.get(field) for field in REVIEW_STATUS_FIELDS])
        style_review_status_worksheet(status_sheet, len(review_statuses or []))

    workbook.save(path)


def export_all(
    output_dir: Path,
    occurrence_path: Path,
    unique_path: Path,
    review_path: Path | None = None,
    review_status_path: Path | None = None,
    include_reviews: bool = False,
) -> None:
    occurrences = read_jsonl(occurrence_path)
    unique_rows = read_jsonl(unique_path)
    write_csv(output_dir / "wayfair_listing_occurrences.csv", occurrences)
    write_csv(output_dir / "wayfair_unique_products.csv", unique_rows)
    reviews = read_jsonl(review_path) if review_path else None
    review_statuses = read_jsonl(review_status_path) if review_status_path else None
    if include_reviews:
        reviews = reviews or []
        review_statuses = review_statuses or []
        write_review_csv(output_dir / "wayfair_reviews.csv", reviews)
        write_review_status_csv(output_dir / "wayfair_review_status.csv", review_statuses)
        write_review_xlsx(output_dir / "wayfair_reviews.xlsx", reviews, review_statuses)
    export_xlsx(
        output_dir / "wayfair_products.xlsx",
        occurrences,
        unique_rows,
        reviews=reviews,
        review_statuses=review_statuses,
    )


# ---------------------------------------------------------------------------
# Keyword search mode: scrape by free-text keyword and group the resulting
# products by the "Narrow Your Search" sub-categories, writing one Excel
# worksheet per category. Uses the same perimeter-safe listing-page fetcher.
# ---------------------------------------------------------------------------

KEYWORD_URL_TMPL = "https://www.wayfair.com/keyword.php?keyword={keyword}"
NON_CATEGORY_CHIPS = {"labor day deal", "fast delivery", "free over $35", "sale",
                      "sort", "featured", "best match", "relevancy", "most popular"}


def keyword_page_url(keyword: str, page_number: int) -> str:
    """Build a Wayfair free-text search URL for a keyword (and its page)."""
    base = KEYWORD_URL_TMPL.format(keyword=quote_plus(keyword))
    return build_page_url(base, page_number)


def slugify_category(label: str) -> str:
    """Normalize a category label into a Wayfair-friendly slug token."""
    s = label.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def keyword_category_phrase(keyword: str, label: str) -> str:
    """Build the refined keyword for a sub-category, e.g. 'wicker storage baskets'."""
    first = keyword.strip().split()[0] if keyword.strip().split() else keyword.strip()
    return f"{first} {slugify_category(label)}"


def extract_narrow_search_categories(html: str) -> list[str]:
    """Extract the 'Narrow Your Search' sub-category labels from a keyword page."""
    soup = BeautifulSoup(html, "lxml")
    labels: list[str] = []
    seen: set[str] = set()
    for chip in soup.select('[data-hb-id="Chip"]'):
        text = re.sub(r"\s+", " ", chip.get_text(" ", strip=True)).strip()
        if not text or len(text) < 3:
            continue
        # Cards render their label twice ("Storage Baskets Storage Baskets").
        half = len(text) // 2
        if text[:half].strip().lower() == text[half:].strip().lower():
            text = text[:half].strip()
        if not text or text.lower() in NON_CATEGORY_CHIPS:
            continue
        if text not in seen:
            seen.add(text)
            labels.append(text)
    return labels


_FILTER_GROUP_STOP = re.compile(
    r"^(\$|under \$|rated \d|all reviewed|wayfair verified)", re.IGNORECASE
)


def extract_filter_category_labels(html: str) -> list[str]:
    """Extract the sidebar 'Filter By Category' checkbox labels.

    On keyword/search pages Wayfair renders sub-categories as plain checkboxes
    (input @name holds the category name, no href). They live in the filter
    sidebar whose text starts with 'Filter By Category'; the leading labels
    before the Price/Rating/Brand groups are the sub-categories.
    """
    soup = BeautifulSoup(html, "lxml")
    for sec in soup.select('[data-hb-id="BoxV3"]'):
        text = " ".join(sec.stripped_strings)
        if not text.startswith("Filter By Category"):
            continue
        labels: list[str] = []
        seen: set[str] = set()
        for box in sec.select("input[type=checkbox][name]"):
            name = (box.get("name") or "").strip()
            if not name:
                continue
            if _FILTER_GROUP_STOP.match(name):
                break
            if name.lower() in NON_CATEGORY_CHIPS:
                continue
            if name not in seen:
                seen.add(name)
                labels.append(name)
        if labels:
            return labels
    return []


def extract_master_cl_ids(html_source: str) -> list[tuple[str, str]]:
    """Map sidebar 'Filter By Category' checkboxes to their masterClID filter
    value, e.g. ('Boxes, Bins, Baskets, & Buckets', '1223').

    Selecting a category yields
    .../filters/keyword.php?keyword=...&filters=masterClID~1223&filtered=true
    so each checkbox's optionId equals the masterClID segment. This lets us
    scrape each sub-category directly (no keyword re-search) over plain HTTP.
    """
    soup = BeautifulSoup(html_source, "lxml")
    for group in soup.select("[data-clio-context]"):
        try:
            gctx = json.loads(group["data-clio-context"])
        except Exception:
            continue
        if not isinstance(gctx, dict):
            continue
        if gctx.get("filterType") == "group" and gctx.get("filterId") == "masterClID":
            out: list[tuple[str, str]] = []
            seen: set[tuple[str, str]] = set()
            for opt in group.select("[data-clio-context]"):
                try:
                    octx = json.loads(opt["data-clio-context"])
                except Exception:
                    continue
                if not isinstance(octx, dict):
                    continue
                if "optionId" not in octx or octx.get("selected") is None:
                    continue
                label = (octx.get("label") or "").strip()
                cid = str(octx.get("optionId") or "").strip()
                if label and cid and (label, cid) not in seen:
                    seen.add((label, cid))
                    out.append((label, cid))
            if out:
                return out
    return []


def category_chip_label(chip: Any) -> str:
    """Normalize one 'Narrow Your Search' chip element into a clean label."""
    text = re.sub(r"\s+", " ", chip.get_text(" ", strip=True)).strip()
    if not text or len(text) < 3:
        return ""
    # Cards render their label twice ("Storage Baskets Storage Baskets").
    half = len(text) // 2
    if text[:half].strip().lower() == text[half:].strip().lower():
        text = text[:half].strip()
    text = text.strip()
    if not text or text.lower() in NON_CATEGORY_CHIPS:
        return ""
    return text


def extract_narrow_search_category_paths(
    html: str,
    base_url: str,
) -> list[tuple[str, str | None]]:
    """Extract (label, url) pairs for 'Narrow Your Search' sub-category chips.

    The chip element link is preferred when present so the refined URL stays
    inside the original category; otherwise the URL is None and the caller
    falls back to a built keyword search.
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[tuple[str, str | None]] = []
    seen_labels: set[str] = set()
    for chip in soup.select('[data-hb-id="Chip"]'):
        label = category_chip_label(chip)
        if label and label not in seen_labels:
            seen_labels.add(label)
            href: str | None = None
            for anchor in chip.select("a[href]"):
                href = str(anchor.get("href") or "").strip()
                break
            if href:
                href = urljoin(base_url, href)
            results.append((label, href))
    return results


def url_slug_words(url: str) -> str:
    """Derive a human-friendly parent title from a category URL, e.g.
    '.../office-chairs-c478390.html' -> 'office chairs'."""
    match = re.search(r"/([\w-]+)-c\d+\.html", url)
    slug = match.group(1) if match else ""
    return slug.replace("-", " ").strip() or "products"


def categorized_parent_title(url: str) -> str:
    """Parent title for classification output. For a keyword search URL the
    search phrase is the natural title; otherwise fall back to the URL slug."""
    query = dict(parse_qsl(urlsplit(url).query))
    keyword = (query.get("keyword") or "").strip()
    return keyword or url_slug_words(url)


def url_category_keyword(url: str, label: str) -> str:
    """Build the refined keyword for a sub-category when the chip has no link,
    e.g. 'office chairs executive chairs'."""
    parent = url_slug_words(url)
    first = parent.split()[0] if parent.split() else parent
    return f"{first} {slugify_category(label)}"


def fetch_search_page(
    curl_path: str | None,
    url: str,
    raw_dir: Path,
    cookie_path: Path,
    timeout: int,
    retries: int,
) -> str:
    """Fetch one Wayfair listing/search page with retry and challenge handling."""
    last_error = ""
    for attempt in range(1, max(1, retries) + 1):
        temp_path = raw_dir / f"kw_{hashlib.sha1(url.encode('utf-8')).hexdigest()[:16]}.html"
        status, last_error = fetch_with_curl(
            curl_path=curl_path,
            page_url=url,
            output_path=temp_path,
            cookie_path=cookie_path,
            timeout=timeout,
        )
        html = ""
        if temp_path.exists():
            html = temp_path.read_text(encoding="utf-8", errors="ignore")
            temp_path.unlink(missing_ok=True)
        if status == 200 and html and not is_challenge_html(html):
            return html
        print(f"  attempt {attempt}/{retries} failed: HTTP {status}; {last_error}")
        time.sleep(min(20 * attempt, 60))
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def excel_sheet_title(name: str) -> str:
    clean = re.sub(r"[\[\]:*?/\\]", " ", name)
    clean = re.sub(r"\s+", " ", clean).strip()
    if len(clean) > 31:
        clean = clean[:28] + "..."
    return clean or "Other"


def export_categorized_xlsx(
    path: Path,
    categories: dict[str, list[dict[str, Any]]],
    keyword: str,
) -> None:
    """Write one worksheet per product category, plus a Summary sheet."""
    workbook = Workbook()
    summary = workbook.active
    summary.title = "Summary"
    summary.append(["Metric", "Value"])
    summary.append(["Keyword", keyword])
    summary.append(["Categories", len(categories)])
    for name, rows in categories.items():
        summary.append([f"Items in '{name}'", len(rows)])
    for cell in summary[1]:
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.font = Font(color="FFFFFF", bold=True)
    summary.column_dimensions["A"].width = 30
    summary.column_dimensions["B"].width = 60
    summary.freeze_panes = "A2"

    for name, rows in categories.items():
        sheet = workbook.create_sheet(title=excel_sheet_title(name))
        sheet.append(HEADERS_ZH)
        for row in rows:
            sheet.append([row.get(field) for field in FIELDS])
        style_worksheet(sheet, len(rows))

    workbook.save(path)


def run_url_categorized_flow(args: argparse.Namespace) -> int:
    """URL category mode that optionally groups products by the 'Narrow Your
    Search' sub-categories, writing one worksheet per category."""
    url = str(args.url).strip()
    if not url:
        raise ValueError("--url must not be empty")
    if args.category_pages < 1:
        raise ValueError("--category-pages must be at least 1")

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw_html"
    raw_dir.mkdir(exist_ok=True)
    curl_path = find_curl()
    cookie_path = output_dir / "cookies.txt"

    parent_title = categorized_parent_title(url)
    print("Category URL:", url)

    first_html = fetch_search_page(
        curl_path, build_page_url(url, 1),
        raw_dir, cookie_path, args.timeout, args.retries,
    )
    # Preferred path: use each sidebar category's masterClID to build a filter URL
    # (.../filters/keyword.php?keyword=X&filters=masterClID~{id}&filtered=true)
    # that returns exactly that sub-category's items over plain HTTP. Falls back
    # to sidebar-label keywords, then top "Narrow Your Search" chips.
    categories: list[tuple[str, str, dict[str, str]]] = []
    kw_param = (dict(parse_qsl(urlsplit(url).query)).get("keyword") or "").strip()
    clids = extract_master_cl_ids(first_html) if kw_param else []
    if clids:
        print(f"[提示] 使用页面侧栏分类 masterClID: {[label for label, _ in clids]}")
        for label, cid in clids:
            base = (
                "https://www.wayfair.com/filters/keyword.php"
                f"?keyword={quote_plus(kw_param)}&filters=masterClID%7E{cid}&filtered=true"
            )
            categories.append((label, base, {"source": "clid", "base": base}))
    else:
        sidebar = extract_filter_category_labels(first_html)
        if sidebar:
            print(f"[提示] 使用页面侧栏 Filter By Category 子分类: {sidebar}")
            for label in sidebar:
                phrase = url_category_keyword(url, label)
                categories.append(
                    (label, phrase, {"source": "keyword", "base": keyword_page_url(phrase, 1)})
                )
        else:
            chips = extract_narrow_search_category_paths(first_html, url)
            for label, href in chips:
                if href:
                    categories.append((label, href, {"source": "url", "base": href}))
                else:
                    phrase = url_category_keyword(url, label)
                    categories.append(
                        (label, phrase, {"source": "keyword", "base": keyword_page_url(phrase, 1)})
                    )

    if not categories:
        print(
            f"[提示] 页面（{parent_title}）未发现可用的子分类 "
            "(Narrow Your Search) 过滤，将按整体抓取第 "
            f"{args.start_page}-{args.end_page} 页并归为单一分类。"
        )
        by_category = {}
        for page in range(args.start_page, args.end_page + 1):
            page_url = build_page_url(url, page)
            html = fetch_search_page(
                curl_path, page_url, raw_dir, cookie_path,
                args.timeout, args.retries,
            )
            page_rows = parse_listing_page(html, page, page_url)
            for row in page_rows:
                row["category"] = parent_title or "Other"
                row["categoryKeyword"] = parent_title
            by_category.setdefault(parent_title or "Other", []).extend(page_rows)
            print(f"  page {page}: {len(page_rows)} items (total "
                  f"{len(by_category[parent_title or 'Other'])})")
            if len(page_rows) < 10:
                print(f"  page {page}: only {len(page_rows)} items; stopping this category")
                break
            if page < args.end_page:
                time.sleep(random.uniform(args.delay_min, args.delay_max))
        write_jsonl(output_dir / "wayfair_categorized_url.jsonl",
                    [r for rows in by_category.values() for r in rows])
        xlsx_path = output_dir / "wayfair_categorized_by_category.xlsx"
        export_categorized_xlsx(xlsx_path, by_category, parent_title)
        print("\nCategory summary:")
        for label, rows in by_category.items():
            print(f"  {label}: {len(rows)} items")
        print(f"Done (no sub-categories found). Output: {xlsx_path}")
        return 0

    print(
        f"Discovered {len(categories)} sub-categories: "
        f"{[label for label, _, _ in categories]}"
    )

    by_category: dict[str, list[dict[str, Any]]] = {}
    assigned_skus: set[str] = set()
    for label, target, info in categories:
        print(f"--- Category: {label}  (source: {info['source']}) ---")
        rows: list[dict[str, Any]] = []
        for page in range(1, max(1, args.category_pages) + 1):
            if info["source"] == "clid":
                page_url = info["base"] if page <= 1 else f"{info['base']}&curpage={page}"
            elif info["source"] == "url":
                page_url = build_page_url(info["base"], page)
            else:
                page_url = keyword_page_url(target, page)
            html = fetch_search_page(
                curl_path, page_url, raw_dir, cookie_path,
                args.timeout, args.retries,
            )
            page_rows = parse_listing_page(html, page, page_url)
            for row in page_rows:
                row["category"] = label
                row["categoryKeyword"] = (
                    target if info["source"] == "keyword" else label
                )
            rows.extend(page_rows)
            assigned_skus.update(r.get("sku") for r in page_rows if r.get("sku"))
            if len(page_rows) < 10:
                print(f"  page {page}: only {len(page_rows)} items; stopping this category")
                break
            print(f"  page {page}: {len(page_rows)} items (total {len(rows)})")
            if page < args.category_pages:
                time.sleep(random.uniform(args.delay_min, args.delay_max))
        by_category[label] = rows

    # Items on the parent category's first page that no sub-category covered.
    try:
        base_rows = parse_listing_page(first_html, 1, build_page_url(url, 1))
        other = [r for r in base_rows if r.get("sku") not in assigned_skus]
        for row in other:
            row["category"] = "Other"
            row["categoryKeyword"] = parent_title
        if other:
            by_category.setdefault("Other", other)
    except Exception:
        pass

    write_jsonl(output_dir / "wayfair_categorized_url.jsonl",
                [r for rows in by_category.values() for r in rows])
    xlsx_path = output_dir / "wayfair_categorized_by_category.xlsx"
    export_categorized_xlsx(xlsx_path, by_category, parent_title)
    print("\nCategory summary:")
    for label, rows in by_category.items():
        print(f"  {label}: {len(rows)} items")
    print(f"Done. Output: {xlsx_path}")
    return 0


def run_keyword_categorized_flow(args: argparse.Namespace) -> int:
    keyword = args.keyword.strip()
    if not keyword:
        raise ValueError("--keyword must not be empty")
    if args.category_pages < 1:
        raise ValueError("--category-pages must be at least 1")

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw_html"
    raw_dir.mkdir(exist_ok=True)
    curl_path = find_curl()
    cookie_path = output_dir / "cookies.txt"
    print("Keyword:", keyword)

    # --- Discover sub-category keywords ---
    categories: list[tuple[str, str]] = []
    if args.category_keywords:
        for raw in args.category_keywords.split(","):
            phrase = raw.strip()
            if phrase:
                categories.append((phrase, phrase))
    else:
        main_html = fetch_search_page(
            curl_path, keyword_page_url(keyword, 1),
            raw_dir, cookie_path, args.timeout, args.retries,
        )
        labels = extract_filter_category_labels(main_html)
        if not labels:
            labels = extract_narrow_search_categories(main_html)
        if not labels:
            raise RuntimeError(
                "No sub-categories could be discovered on the keyword page. "
                "Pass --category-keywords to list them explicitly."
            )
        print(f"[提示] 使用页面侧栏 Filter By Category 子分类: {labels}")
        for label in labels:
            categories.append((label, keyword_category_phrase(keyword, label)))
        print(f"Discovered {len(categories)} sub-categories: "
              f"{[label for label, _ in categories]}")

    by_category: dict[str, list[dict[str, Any]]] = {}
    assigned_skus: set[str] = set()
    for label, phrase in categories:
        print(f"--- Category: {label}  (refined keyword: {phrase}) ---")
        rows: list[dict[str, Any]] = []
        for page in range(1, max(1, args.category_pages) + 1):
            page_url = keyword_page_url(phrase, page)
            html = fetch_search_page(
                curl_path, page_url, raw_dir, cookie_path,
                args.timeout, args.retries,
            )
            page_rows = parse_listing_page(html, page, page_url)
            for row in page_rows:
                row["category"] = label
                row["categoryKeyword"] = phrase
            rows.extend(page_rows)
            assigned_skus.update(r.get("sku") for r in page_rows if r.get("sku"))
            if len(page_rows) < 10:
                print(f"  page {page}: only {len(page_rows)} items; stopping this category")
                break
            print(f"  page {page}: {len(page_rows)} items (total {len(rows)})")
            if page < args.category_pages:
                time.sleep(random.uniform(args.delay_min, args.delay_max))
        by_category[label] = rows

    # Base-keyword items not captured by any sub-category land in "Other".
    try:
        main_html = fetch_search_page(
            curl_path, keyword_page_url(keyword, 1),
            raw_dir, cookie_path, args.timeout, args.retries,
        )
        base_rows = parse_listing_page(main_html, 1, keyword_page_url(keyword, 1))
        other = [r for r in base_rows if r.get("sku") not in assigned_skus]
        for row in other:
            row["category"] = "Other"
        if other:
            by_category.setdefault("Other", other)
    except Exception:
        # Categories are the primary deliverable; base fallback is optional.
        pass

    write_jsonl(output_dir / "wayfair_categorized.jsonl",
                [r for rows in by_category.values() for r in rows])
    xlsx_path = output_dir / "wayfair_categorized_by_keyword.xlsx"
    export_categorized_xlsx(xlsx_path, by_category, keyword)
    summary = [
        f"{label}: {len(rows)} items"
        for label, rows in by_category.items()
    ]
    print("\nCategory summary:")
    for line in summary:
        print("  " + line)
    print(f"Done. Output: {xlsx_path}")
    print("JSONL (all categorized rows):", output_dir / "wayfair_categorized.jsonl")
    return 0


def main() -> int:
    args = parse_args()
    if args.keyword:
        return run_keyword_categorized_flow(args)
    if args.categorize:
        if args.reviews_only:
            raise ValueError("--categorize cannot be combined with --reviews-only")
        return run_url_categorized_flow(args)
    if args.start_page < 1 or args.end_page < args.start_page:
        raise ValueError("Invalid page range")
    if args.reviews_per_product < 0:
        raise ValueError("--reviews-per-product must be zero or greater")
    if args.review_page_size < 1:
        raise ValueError("--review-page-size must be at least 1")
    if args.review_workers < 1:
        raise ValueError("--review-workers must be at least 1")
    if args.review_timeout < 1 or args.review_retries < 1:
        raise ValueError("Review timeout and retries must be at least 1")
    if args.review_headless and not args.review_browser:
        raise ValueError("--review-headless requires --review-browser")
    if args.cookie_header_file and not args.review_browser:
        raise ValueError("--cookie-header-file requires --review-browser")
    if args.reviews_only and not args.scrape_reviews:
        raise ValueError("--reviews-only requires --scrape-reviews")

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw_html"
    raw_dir.mkdir(exist_ok=True)

    occurrence_path = output_dir / "wayfair_listing_occurrences.jsonl"
    unique_path = output_dir / "wayfair_unique_products.jsonl"
    checkpoint_path = output_dir / "checkpoint_http.json"
    cookie_path = output_dir / "cookies.txt"
    review_cookie_path = (
        Path(args.cookie_header_file).resolve()
        if args.cookie_header_file
        else cookie_path
    )
    if args.cookie_header_file and not review_cookie_path.exists():
        raise FileNotFoundError(f"Cookie header file was not found: {review_cookie_path}")
    review_path = output_dir / "wayfair_reviews.jsonl"
    review_status_path = output_dir / "wayfair_review_status.jsonl"

    if args.fresh:
        reset_paths = [review_path, review_status_path]
        if not args.reviews_only:
            reset_paths = [
                occurrence_path,
                unique_path,
                checkpoint_path,
                cookie_path,
                *reset_paths,
            ]
        for path in reset_paths:
            path.unlink(missing_ok=True)

    occurrences = read_jsonl(occurrence_path)
    unique_existing = read_jsonl(unique_path)
    if args.reviews_only and not unique_path.exists():
        raise FileNotFoundError(
            "Review-only mode requires an existing product list: "
            f"{unique_path}"
        )
    seen_unique = {row.get("sku") for row in unique_existing if row.get("sku")}
    global_rank = len(occurrences)

    next_page = args.start_page
    if checkpoint_path.exists() and not args.fresh:
        state = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if state.get("categoryUrl") == args.url:
            next_page = max(args.start_page, int(state.get("nextPage", args.start_page)))

    curl_path = None if args.offline_html_dir or args.reviews_only else find_curl()
    offline_dir = Path(args.offline_html_dir).resolve() if args.offline_html_dir else None
    offline_product_dir = (
        Path(args.offline_product_html_dir).resolve()
        if args.offline_product_html_dir
        else None
    )

    pages_completed = 0
    page_numbers = [] if args.reviews_only else range(next_page, args.end_page + 1)
    for page_number in page_numbers:
        page_url = build_page_url(args.url, page_number)
        temp_path = raw_dir / f"page_{page_number:03d}.html.tmp"
        print(f"[{page_number}/{args.end_page}] {page_url}")

        if offline_dir:
            candidates = [
                offline_dir / f"wayfair_page{page_number}.html",
                offline_dir / f"page_{page_number}.html",
                offline_dir / f"page_{page_number:03d}.html",
            ]
            source_path = next((path for path in candidates if path.exists()), None)
            if source_path is None:
                raise FileNotFoundError(f"Offline HTML for page {page_number} was not found")
            html = source_path.read_text(encoding="utf-8", errors="ignore")
        else:
            html = ""
            last_error = ""
            for attempt in range(1, args.retries + 1):
                status, last_error = fetch_with_curl(
                    curl_path=curl_path,
                    page_url=page_url,
                    output_path=temp_path,
                    cookie_path=cookie_path,
                    timeout=args.timeout,
                )
                if temp_path.exists():
                    html = temp_path.read_text(encoding="utf-8", errors="ignore")
                if status == 200 and html and not is_challenge_html(html):
                    break
                print(f"  attempt {attempt}/{args.retries} failed: HTTP {status}; {last_error}")
                time.sleep(min(20 * attempt, 60))
            else:
                failed_path = output_dir / f"failed_page_{page_number}.html"
                if temp_path.exists():
                    shutil.copy2(temp_path, failed_path)
                raise RuntimeError(
                    f"Page {page_number} failed or returned a challenge. Saved: {failed_path}"
                )

        rows = parse_listing_page(html, page_number, page_url)
        if len(rows) < 30:
            failed_path = output_dir / f"unexpected_page_{page_number}.html"
            failed_path.write_text(html, encoding="utf-8")
            raise RuntimeError(
                f"Only {len(rows)} listing cards were parsed on page {page_number}. Saved: {failed_path}"
            )

        occurrence_rows: list[dict[str, Any]] = []
        unique_rows: list[dict[str, Any]] = []
        for row in rows:
            global_rank += 1
            row["globalRank"] = global_rank
            occurrence_rows.append(row)
            if row["sku"] not in seen_unique:
                seen_unique.add(row["sku"])
                unique_rows.append(dict(row))

        append_jsonl(occurrence_path, occurrence_rows)
        append_jsonl(unique_path, unique_rows)

        checkpoint_path.write_text(
            json.dumps(
                {
                    "categoryUrl": args.url,
                    "nextPage": page_number + 1,
                    "occurrenceCount": global_rank,
                    "uniqueCount": len(seen_unique),
                    "updatedAt": now_iso(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        if args.keep_html:
            with gzip.open(raw_dir / f"page_{page_number:03d}.html.gz", "wt", encoding="utf-8") as handle:
                handle.write(html)
        temp_path.unlink(missing_ok=True)

        pages_completed += 1
        print(
            f"  parsed {len(rows)} displayed products; "
            f"new unique {len(unique_rows)}; total occurrences {global_rank}; "
            f"total unique {len(seen_unique)}"
        )

        if args.export_every > 0 and pages_completed % args.export_every == 0:
            export_all(
                output_dir,
                occurrence_path,
                unique_path,
                review_path=review_path,
                review_status_path=review_status_path,
                include_reviews=args.scrape_reviews,
            )
            print("  checkpoint Excel/CSV exported")

        if page_number < args.end_page and not offline_dir:
            time.sleep(random.uniform(args.delay_min, args.delay_max))

    if args.scrape_reviews:
        all_unique_rows = read_jsonl(unique_path)
        existing_reviews = read_jsonl(review_path)
        existing_statuses = read_jsonl(review_status_path)
        completed_skus = {
            str(row.get("sku"))
            for row in existing_statuses
            if row.get("status") in {"ok", "no_reviews"} and row.get("sku")
        }
        pending_products = [
            row for row in all_unique_rows if str(row.get("sku") or "") not in completed_skus
        ]

        if pending_products:
            print(f"Starting review scrape: {len(pending_products)} products")
            new_reviews, new_statuses = scrape_reviews_for_products(
                products=pending_products,
                output_dir=output_dir,
                cookie_path=review_cookie_path,
                review_path=review_path,
                review_status_path=review_status_path,
                max_reviews=args.reviews_per_product,
                page_size=args.review_page_size,
                workers=args.review_workers,
                timeout=args.review_timeout,
                retries=args.review_retries,
                offline_dir=offline_product_dir,
                browser_mode=args.review_browser,
                browser_headless=args.review_headless,
            )
        else:
            new_reviews, new_statuses = [], []
            print("Review scrape checkpoint is complete; no products need re-fetching.")

        review_map: dict[tuple[str, str], dict[str, Any]] = {}
        for row in existing_reviews + new_reviews:
            key = (str(row.get("sku") or ""), str(row.get("reviewId") or ""))
            if key[0] and key[1]:
                review_map[key] = row

        status_map: dict[str, dict[str, Any]] = {}
        for row in existing_statuses + new_statuses:
            sku = str(row.get("sku") or "")
            if sku:
                status_map[sku] = row

        ordered_reviews = list(review_map.values())
        ordered_statuses = [
            status_map[str(row.get("sku"))]
            for row in all_unique_rows
            if str(row.get("sku") or "") in status_map
        ]
        write_jsonl(review_path, ordered_reviews)
        write_jsonl(review_status_path, ordered_statuses)

    export_all(
        output_dir,
        occurrence_path,
        unique_path,
        review_path=review_path,
        review_status_path=review_status_path,
        include_reviews=args.scrape_reviews,
    )
    print(f"Done. Output: {output_dir / 'wayfair_products.xlsx'}")
    if args.scrape_reviews:
        print(f"Reviews output: {output_dir / 'wayfair_reviews.csv'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Stopped by user. JSONL and checkpoint data were preserved.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
