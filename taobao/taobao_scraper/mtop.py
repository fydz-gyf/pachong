"""MTop 搜索接口的请求构造、错误分类与重试。

错误分为四类，处理方式各不相同：
- 风控 / 频率验证：长退避，不刷新 Cookie（刷新只会加重限制）
- 后端临时超时：短退避，同一页重试，保持分页状态
- 网络 / TLS：重建连接池后重试
- Token / Session 过期：重新签名，或从 CDP 刷新登录态
"""

from __future__ import annotations

import json
import logging
import time
from urllib.parse import quote

import requests

from . import auth, cdp
from .config import (
    API_NAME,
    API_URL,
    API_VERSION,
    APP_ID,
    APP_KEY,
    Settings,
    initial_paging_state,
)
from .cooling import pick_backoff, random_sleep
from .errors import (
    MTopServerTimeoutError,
    NetworkTransportError,
    RiskLimitError,
)

PAGING_KEYS = (
    "sourceS",
    "bcoffset",
    "ntoffset",
    "totalPage",
    "totalResults",
    "sessionid",
)


# ============================================================
# 响应解析与错误分类
# ============================================================

def parse_jsonp(text: str) -> dict:
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)

    first = text.find("(")
    last = text.rfind(")")
    if first < 0 or last <= first:
        raise ValueError("无法识别 MTop JSON/JSONP 响应: " + text[:300])
    return json.loads(text[first + 1 : last])


def is_mtop_success(obj: dict) -> bool:
    return any(str(x).startswith("SUCCESS") for x in obj.get("ret", []))


def ret_text(obj: dict) -> str:
    return " ".join(str(x) for x in obj.get("ret", [])).upper()


def is_risk_validate_error(obj: dict) -> bool:
    """淘宝访问频率/风控验证。刷新 Cookie 通常无效，应该退避等待。"""
    text = ret_text(obj)
    markers = [
        "FAIL_SYS_USER_VALIDATE",
        "RGV587",
        "USER_VALIDATE",
    ]
    return any(m in text for m in markers)


def is_server_transient_error(obj: dict) -> bool:
    """淘宝 MTop/TPP 后端临时故障。

    典型返回：SOLUTION_EXECUTE_TIMEOUT、TPP RPC Timeout。
    同一页稍后重试即可，不应刷新 Cookie，也不应结束整个关键词。
    """
    text = ret_text(obj)
    markers = [
        "SOLUTION_EXECUTE_TIMEOUT",
        "TPPERRORCODE-[SOLUTION_EXECUTE_TIMEOUT]",
        "COM.ALIBABA.TPP.RPC.CLIENT.TIMEOUTEXCEPTION",
        "TIMEOUT AFTER 1000MS",
    ]
    return any(m in text for m in markers)


def is_auth_or_token_error(obj: dict) -> bool:
    """真正的 token/session 过期类错误。"""
    text = ret_text(obj)
    markers = [
        "TOKEN",
        "SESSION_EXPIRED",
        "ILLEGAL_ACCESS",
        "EXPIRED",
        "FAIL_SYS_SESSION_EXPIRED",
    ]
    return any(m in text for m in markers)


def reset_requests_connection_pool(session: requests.Session) -> None:
    """
    丢弃 requests/urllib3 现有连接池，但保留当前 Session 的 Cookie 和 headers。

    SSLEOFError 后继续复用旧连接池没有意义；重新 mount adapter 可以强制
    下一次请求建立全新的 TCP/TLS 连接。
    """
    for prefix in ("https://", "http://"):
        old_adapter = session.adapters.get(prefix)
        if old_adapter is not None:
            try:
                old_adapter.close()
            except Exception:
                pass

        session.mount(
            prefix,
            requests.adapters.HTTPAdapter(
                pool_connections=8,
                pool_maxsize=8,
                max_retries=0,
                pool_block=False,
            ),
        )


# ============================================================
# 搜索请求参数
# ============================================================

def build_search_data(
    keyword: str,
    page_no: int,
    environment: dict,
    cna: str,
    paging_state: dict,
) -> str:
    # 与已经成功复现的请求保持一致：q 在 params JSON 内先 percent-encode
    encoded_keyword = quote(keyword, safe="")

    params = {
        "device": "HMA-AL00",
        "isBeta": "false",
        "grayHair": "false",
        "from": "nt_history",
        "brand": "HUAWEI",
        "info": "wifi",
        "index": "4",
        "rainbow": "",
        "schemaType": "auction",
        "elderHome": "false",
        "isEnterSrpSearch": "true",
        "newSearch": "false",
        "network": "wifi",
        "subtype": "",
        "hasPreposeFilter": "false",
        "prepositionVersion": "v2",
        "client_os": "Android",
        "gpsEnabled": "false",
        "searchDoorFrom": "srp",
        "debug_rerankNewOpenCard": "false",
        "homePageVersion": "v7",
        "searchElderHomeOpen": "false",
        "search_action": "initiative",
        "sugg": "_4_1",
        "sversion": "13.6",
        "style": "list",
        "ttid": "600000@taobao_pc_10.7.0",
        "needTabs": "true",
        "areaCode": "CN",
        "vm": "nw",
        "countryNum": "156",
        "m": "pc",

        # 搜索核心
        "page": page_no,
        "n": 48,
        "q": encoded_keyword,
        "qSource": "url",
        "pageSource": "",
        "channelSrp": "",
        "tab": "all",
        "pageSize": 48,
        "totalPage": paging_state.get("totalPage", "100"),
        "totalResults": paging_state.get("totalResults", "4800"),
        "sourceS": paging_state.get("sourceS", "0"),
        "sort": "_coefp",
        "bcoffset": paging_state.get("bcoffset", ""),
        "ntoffset": paging_state.get("ntoffset", ""),

        # 筛选默认空
        "filterTag": "",
        "service": "",
        "prop": "",
        "loc": "",
        "start_price": None,
        "end_price": None,
        "startPrice": None,
        "endPrice": None,
        "itemIds": None,
        "p4pIds": None,
        "p4pS": None,
        "categoryp": "",
        "ha3Kvpairs": None,

        # 环境
        "myCNA": cna,
        "screenResolution": environment["screenResolution"],
        "viewResolution": environment["viewResolution"],
        "userAgent": environment["userAgent"],
        "couponUnikey": "",
        "subTabId": "",
        "np": "",
        "clientType": "h5",
        "isNewDomainAb": "false",
        "forceOldDomain": "false",
    }

    # 注意：当前已验证成功的请求没有主动携带 sessionid。
    # mainInfo 里的 sessionid 只保存在状态中用于诊断，不主动加回请求，
    # 避免改变已经验证通过的请求形态。

    params_string = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
    outer = {"appId": APP_ID, "params": params_string}
    return json.dumps(outer, ensure_ascii=False, separators=(",", ":"))


def update_paging_state(obj: dict, state: dict) -> dict:
    data = obj.get("data") or {}
    main_info = data.get("mainInfo") or {}

    for key in PAGING_KEYS:
        value = main_info.get(key)
        if value is not None and value != "":
            state[key] = str(value)

    return state


# ============================================================
# MTop 请求
# ============================================================

def call_mtop_search(
    session: requests.Session,
    environment: dict,
    keyword: str,
    page_no: int,
    paging_state: dict,
    settings: Settings,
) -> tuple[dict, float, dict]:
    """
    返回 (obj, elapsed, environment)。

    environment 可能在 token 刷新时被替换，因此回传给调用方。
    """
    risk_failures = 0
    network_failures = 0
    server_failures = 0

    for attempt in range(1, settings.max_request_retries + 1):
        token = auth.get_mtop_token(session)
        if not token:
            logging.warning("Session 中没有 _m_h5_tk，尝试从 CDP 更新 Cookie")
            environment = auth.refresh_session_from_cdp(session, settings)
            token = auth.get_mtop_token(session)
            if not token:
                raise RuntimeError("更新 Cookie 后仍没有 _m_h5_tk")

        cna = auth.get_cookie_value(session, "cna")
        data_string = build_search_data(
            keyword=keyword,
            page_no=page_no,
            environment=environment,
            cna=cna,
            paging_state=paging_state,
        )

        timestamp = int(time.time() * 1000)
        sign = auth.calculate_sign(token, timestamp, data_string)
        callback = f"mtopjsonp{timestamp % 100000}"

        query = {
            "jsv": "2.7.4",
            "appKey": APP_KEY,
            "t": str(timestamp),
            "sign": sign,
            "api": API_NAME,
            "v": API_VERSION,
            "timeout": "10000",
            "type": "jsonp",
            "dataType": "jsonp",
            "callback": callback,
            "data": data_string,
            "bx-ua": "fast-load",
        }

        referer = (
            "https://s.taobao.com/search"
            f"?page={page_no}&q={quote(keyword)}&tab=all"
        )

        logging.info(
            f"[{keyword}] 请求第 {page_no} 页，尝试 {attempt}/{settings.max_request_retries}"
        )
        start = time.perf_counter()

        try:
            response = session.get(
                API_URL,
                params=query,
                headers={
                    "Referer": referer,
                    # MTop 每页请求并不依赖长连接；遇到某些网络环境时，
                    # 主动关闭连接比复用异常 TLS 连接更稳。
                    "Connection": "close",
                },
                timeout=(8, settings.request_timeout),
            )
        except requests.exceptions.RequestException as e:
            elapsed = time.perf_counter() - start
            network_failures += 1

            # 只打印异常类型和简短信息，避免把几千字符的签名 URL 刷满控制台。
            short_error = str(e).split(" with url:", 1)[0]
            if len(short_error) > 260:
                short_error = short_error[:260] + "..."

            if attempt >= settings.max_request_retries:
                raise NetworkTransportError(
                    f"第 {page_no} 页 MTop 网络/TLS 连续失败 "
                    f"{settings.max_request_retries} 次："
                    f"{type(e).__name__}: {short_error}"
                ) from e

            wait_seconds = pick_backoff(
                settings.network_backoff_ranges, network_failures
            )
            logging.warning(
                f"[{keyword}] 第{page_no}页发生网络/TLS异常 "
                f"({type(e).__name__})，{elapsed:.2f}s；"
                f"重建连接池后等待 {wait_seconds:.1f}s 再试"
            )

            reset_requests_connection_pool(session)
            time.sleep(wait_seconds)
            continue

        elapsed = time.perf_counter() - start

        try:
            obj = parse_jsonp(response.text)
        except Exception as e:
            raise RuntimeError(
                f"MTop 响应解析失败，HTTP={response.status_code}, "
                f"body={response.text[:500]}"
            ) from e

        ret = obj.get("ret", [])
        logging.info(
            f"[{keyword}] HTTP {response.status_code} | {elapsed:.2f}s | "
            f"{len(response.content)/1024:.1f}KB | ret={ret}"
        )

        if is_mtop_success(obj):
            auth.save_auth_state(session, environment, settings)
            return obj, elapsed, environment

        # ----------------------------------------------------
        # 风控/访问频率验证：不要把它误当成 token 失效。
        # ----------------------------------------------------
        if is_risk_validate_error(obj):
            risk_failures += 1
            if attempt >= settings.max_request_retries:
                raise RiskLimitError(
                    f"第 {page_no} 页连续触发淘宝访问验证，"
                    f"已重试 {settings.max_request_retries} 次；断点会保留在本页"
                )

            wait_seconds = pick_backoff(settings.risk_backoff_ranges, risk_failures)
            logging.warning(
                f"[{keyword}] 第{page_no}页触发淘宝风控/频率验证，"
                f"等待 {wait_seconds:.1f}s 后再试；不会反复刷新 Cookie"
            )
            # 风控不是 Cookie 过期，这里不刷新 CDP Cookie，避免无效扰动。
            time.sleep(wait_seconds)
            continue

        # ----------------------------------------------------
        # 淘宝 MTop/TPP 后端临时超时，保持分页状态重试同一页。
        # ----------------------------------------------------
        if is_server_transient_error(obj):
            server_failures += 1
            if attempt >= settings.max_request_retries:
                raise MTopServerTimeoutError(
                    f"第 {page_no} 页淘宝后端连续超时 "
                    f"{settings.max_request_retries} 次；"
                    f"断点会保留在本页，下次可继续"
                )

            wait_seconds = pick_backoff(
                settings.server_backoff_ranges, server_failures
            )
            logging.warning(
                f"[{keyword}] 第{page_no}页淘宝 MTop/TPP 后端计算超时 "
                f"(SOLUTION_EXECUTE_TIMEOUT)，等待 {wait_seconds:.1f}s 后重试同一页；"
                f"不刷新 Cookie，不改变分页状态"
            )
            time.sleep(wait_seconds)
            continue

        # ----------------------------------------------------
        # 真正 token/session 过期
        # ----------------------------------------------------
        if is_auth_or_token_error(obj):
            new_token = auth.get_mtop_token(session)

            if new_token and new_token != token:
                logging.warning("MTop 已下发新 token，正在重新签名重试")
                auth.save_auth_state(session, environment, settings)
                random_sleep(0.6, 1.2)
                continue

            if settings.use_cdp_fallback and cdp.cdp_available(
                settings.cdp_host, settings.cdp_port
            ):
                logging.warning("Token/Session 异常，从 Chrome CDP 刷新登录状态")
                environment = auth.refresh_session_from_cdp(session, settings)
                random_sleep(0.8, 1.5)
                continue

        raise RuntimeError(f"MTop 请求失败: {ret}")

    raise RuntimeError("MTop 请求达到最大重试次数")
