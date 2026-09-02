from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import zlib
from typing import Any

from .des import cbc_decrypt, pkcs7_unpad

from ..config import Settings
from ..parsers.search import find_item_stacks, find_search_result
from ..utils import first_nonempty, get_path, normalize_walmart_url, to_number


class SorftimeError(RuntimeError):
    pass


def _gzip_b64(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=9, mtime=0)
    return base64.b64encode(compressed).decode("ascii")


def _decrypt_response_envelope(obj: Any) -> Any:
    """Decode Sorftime's {k,d} response envelope.

    Extension algorithm (verified from the user's installed Sorftime source):
    - key = UTF-8 bytes of k (8 bytes)
    - iv = Base64(MD5(k))[2:10]
    - DES-CBC + PKCS7
    - decrypted bytes are gzip/zlib data
    - inflate -> UTF-8 JSON
    """
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except Exception:
            return obj
    if not isinstance(obj, dict) or not obj.get("k") or not obj.get("d"):
        return obj

    key_text = str(obj["k"])
    key = key_text.encode("utf-8")
    if len(key) != 8:
        raise SorftimeError(f"Unexpected Sorftime DES key length: {len(key)}")
    iv = base64.b64encode(hashlib.md5(key).digest()).decode("ascii")[2:10].encode("ascii")
    cipher_text = base64.b64decode(str(obj["d"]))
    plain_padded = cbc_decrypt(cipher_text, key, iv)
    plain = pkcs7_unpad(plain_padded, 8)
    try:
        # pako.inflate accepts both zlib and gzip wrappers; wbits=47 mirrors that.
        inflated = zlib.decompress(plain, wbits=47)
    except Exception as e:
        raise SorftimeError(f"Sorftime response inflate failed: {e}") from e
    try:
        return json.loads(inflated.decode("utf-8"))
    except Exception as e:
        raise SorftimeError(f"Sorftime response JSON decode failed: {e}") from e


def _flatten_product_items(next_data: dict) -> list[dict]:
    items: list[dict] = []
    for stack in find_item_stacks(next_data):
        if not isinstance(stack, dict):
            continue
        for item in stack.get("items", []) if isinstance(stack.get("items"), list) else []:
            if not isinstance(item, dict):
                continue
            typename = str(item.get("__typename") or "")
            if typename and typename != "Product":
                continue
            if item.get("usItemId") and item.get("name"):
                items.append(item)
    return items


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
            if str(row.get("key") or "").upper() in wanted_keys:
                n = _nonzero_number(row.get("value"))
                if n is not None:
                    return n
    return None


def _raw_price(item: dict):
    pi = item.get("priceInfo") if isinstance(item.get("priceInfo"), dict) else {}
    n = _price_line_value(
        pi,
        ("DISCOUNTED_PRICE", "CURRENT_PRICE", "BASE_PRICE", "FINAL_PRICE"),
        ("PRICE", "CURRENT_PRICE"),
    )
    if n is not None:
        return float(n)
    for value in (
        get_path(pi, "currentPrice", "price"),
        get_path(pi, "currentPrice", "priceString"),
        pi.get("itemPrice"),
        pi.get("linePrice"),
        item.get("price"),
    ):
        n = _nonzero_number(value)
        if n is not None:
            return float(n)
    return 0.0


def _raw_image(item: dict) -> str:
    for value in (
        get_path(item, "imageInfo", "thumbnailUrl"),
        item.get("image"),
        item.get("imageUrl"),
        item.get("primaryImageUrl"),
    ):
        url = normalize_walmart_url(value)
        if url:
            return url
    return ""


def _raw_node_id(item: dict) -> str:
    raw = str(get_path(item, "category", "categoryPathId", default="") or "")
    if not raw:
        return ""
    parts = [x for x in raw.split(":") if x]
    if parts and parts[0] == "0":
        parts = parts[1:]
    return "_".join(parts)


def _raw_rating(item: dict):
    return to_number(
        first_nonempty(
            item.get("averageRating"),
            get_path(item, "rating", "averageRating"),
            get_path(item, "rating", "rating"),
            0,
        )
    ) or 0


def _raw_reviews(item: dict):
    return to_number(
        first_nonempty(
            item.get("numberOfReviews"),
            item.get("reviewCount"),
            get_path(item, "rating", "numberOfReviews"),
            get_path(item, "rating", "count"),
            0,
        )
    ) or 0


def build_sorftime_payload(
    next_data: dict, keyword: str, page_no: int, url: str, nmversion: int
) -> tuple[dict, list[str], dict[str, float]]:
    raw_items = _flatten_product_items(next_data)
    if not raw_items:
        raise SorftimeError("No raw Walmart Product items found for Sorftime request")

    sr = find_search_result(next_data) or {}
    total_count = first_nonempty(
        sr.get("aggregatedCount"),
        sr.get("count"),
        sr.get("totalItemCount"),
        len(raw_items),
    )

    envelope = {
        "Data": {
            "type": "page",
            "content": "search",
            "page": int(page_no),
            "key": keyword,
            "count": int(to_number(total_count) or len(raw_items)),
            "items": raw_items,
        },
        "dataSources": 9,
        "Url": url,
        "IsLogin": False,
    }

    compact = []
    item_ids: list[str] = []
    price_map: dict[str, float] = {}
    for item in raw_items:
        item_id = str(item.get("usItemId") or "")
        if not item_id:
            continue
        item_ids.append(item_id)
        price = _raw_price(item)
        price_map[item_id] = float(price or 0)
        compact.append(
            {
                "asin": item_id,
                "Price": price,
                "Rate": _raw_rating(item),
                "CommentCount": int(to_number(_raw_reviews(item)) or 0),
                "NodeId": _raw_node_id(item),
                "GoodsName": str(item.get("name") or ""),
                "Image": _raw_image(item),
                "Brand": str(first_nonempty(item.get("brand"), item.get("brandName"), "") or ""),
            }
        )

    payload = {
        "Site": 21,
        "JsonData": _gzip_b64(envelope),
        "DataJson": _gzip_b64(compact),
        "_owner": "21",
        "sysLang": "zh-CN",
        "nmversion": int(nmversion),
    }
    return payload, item_ids, price_map


class SorftimeHttpClient:
    ENDPOINT = "https://save.sorftime.com/api/fast/ASINCombinationDetailsMake?site=21&nodeId="

    def __init__(self, settings: Settings, browser, session, environment: dict):
        self.settings = settings
        self.browser = browser
        self.session = session
        self.environment = environment
        self._token = str(getattr(settings, "sorftime_token", "") or "").strip()

    def _resolve_token(self, force: bool = False) -> str:
        if self._token and not force:
            return self._token
        import os

        env_token = str(os.environ.get("SORFTIME_TOKEN") or "").strip()
        if env_token:
            self._token = env_token
            return self._token

        if not self.browser.available():
            raise SorftimeError(
                "Sorftime HTTP mode needs AdsPower once at startup to read the logged-in extension token. "
                "Open the Walmart AdsPower profile with Sorftime logged in, or set SORFTIME_TOKEN."
            )
        try:
            token = self.browser.read_extension_storage(
                self.settings.sorftime_extension_id,
                "sowrftrimyevercity",
            )
        except Exception as e:
            raise SorftimeError(f"Could not read Sorftime login token from extension storage: {e}") from e
        token = str(token or "").strip()
        if not token:
            raise SorftimeError("Sorftime extension is not logged in (token was empty)")
        self._token = token
        return token

    def _headers(self, token: str, refer_url: str) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "nmversion": str(self.settings.sorftime_nmversion),
            "Owner": "21",
            "AuthProvider": "1",
            "Authorization": "BasicAuth " + token,
            "Accept-Language": "zh-CN",
            "referurl": refer_url,
            "Referer": "",
            "User-Agent": self.environment.get("userAgent", ""),
        }

    def fetch_metrics(self, keyword: str, page_no: int, url: str, next_data: dict) -> dict[str, dict[str, Any]]:
        payload, item_ids, price_map = build_sorftime_payload(
            next_data,
            keyword,
            page_no,
            url,
            self.settings.sorftime_nmversion,
        )
        token = self._resolve_token()
        headers = self._headers(token, url)

        kwargs: dict[str, Any] = {
            "headers": headers,
            "json": payload,
            "timeout": self.settings.request_timeout,
        }
        if self.settings.proxy_url:
            kwargs["proxies"] = {"http": self.settings.proxy_url, "https": self.settings.proxy_url}

        response = self.session.post(self.ENDPOINT, **kwargs)
        status = int(getattr(response, "status_code", 0) or 0)
        text = str(getattr(response, "text", "") or "")
        if status in {401, 403}:
            # Token may have rotated. Refresh it from the extension once.
            token = self._resolve_token(force=True)
            headers = self._headers(token, url)
            kwargs["headers"] = headers
            response = self.session.post(self.ENDPOINT, **kwargs)
            status = int(getattr(response, "status_code", 0) or 0)
            text = str(getattr(response, "text", "") or "")
        if status != 200:
            raise SorftimeError(f"Sorftime HTTP {status}: {text[:300]}")

        try:
            raw_obj = json.loads(text)
        except Exception as e:
            raise SorftimeError(f"Sorftime returned non-JSON data: {text[:200]}") from e
        decoded = _decrypt_response_envelope(raw_obj)
        if not isinstance(decoded, dict):
            raise SorftimeError("Sorftime decoded response is not an object")
        code = decoded.get("Code")
        if code not in (0, "0", None):
            raise SorftimeError(f"Sorftime API error Code={code}: {decoded.get('Message')}")
        rows = decoded.get("Data") or []
        if not isinstance(rows, list):
            rows = []

        metrics: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            item_id = str(row.get("ASIN") or "")
            if not item_id:
                continue
            sale_count = int(to_number(row.get("SaleCount")) or 0)
            price = to_number(row.get("Price"))
            if price in ("", None, 0, 0.0):
                price = price_map.get(item_id, 0.0)
            if sale_count > 0:
                sales_value: Any = sale_count
                revenue_value: Any = round(float(price or 0) * sale_count, 2) if price else ""
                status_text = "estimate"
            else:
                sales_value = ""
                revenue_value = ""
                status_text = "unavailable"
            metrics[item_id] = {
                "sorftime_checked": True,
                "sorftime_status": status_text,
                "sorftime_month_sales": sales_value,
                "sorftime_month_revenue": revenue_value,
                "sorftime_month_sales_raw": f"{sale_count:,}" if sale_count > 0 else "--",
                "sorftime_month_revenue_raw": (
                    f"${revenue_value:,.2f}" if revenue_value not in ("", None) else "--"
                ),
            }

        # API usually returns one row per request item. Mark rows missing from Data as unchecked at merge time.
        logging.info(
            "[%s] page %s Sorftime HTTP: requested=%s returned=%s estimates=%s",
            keyword,
            page_no,
            len(set(item_ids)),
            len(metrics),
            sum(1 for x in metrics.values() if x["sorftime_status"] == "estimate"),
        )
        return metrics
