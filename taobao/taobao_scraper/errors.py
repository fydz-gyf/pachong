"""异常类型与风控文案识别。

放在独立模块是为了让 cooling / checkpoint 等模块判断"上次是不是风控失败"
时不必依赖 mtop（那会连带引入 requests 依赖链）。
"""

from __future__ import annotations


class RiskLimitError(RuntimeError):
    """淘宝访问频率/风控验证（FAIL_SYS_USER_VALIDATE / RGV587）。"""


class NetworkTransportError(RuntimeError):
    """MTop 请求在 TCP/TLS/HTTP 传输层连续失败。"""


class MTopServerTimeoutError(RuntimeError):
    """淘宝 MTop/TPP 后端连续计算超时（SOLUTION_EXECUTE_TIMEOUT）。"""


class AuthStateError(RuntimeError):
    """登录态不可用，且无法通过 CDP 恢复。"""


RISK_MARKERS = (
    "FAIL_SYS_USER_VALIDATE",
    "RGV587",
    "USER_VALIDATE",
    "访问验证",
)


def is_risk_message(text: object) -> bool:
    """判断一段文本（通常是 checkpoint 里的 last_error）是否描述风控失败。"""
    upper = str(text or "").upper()
    return any(marker.upper() in upper for marker in RISK_MARKERS)
