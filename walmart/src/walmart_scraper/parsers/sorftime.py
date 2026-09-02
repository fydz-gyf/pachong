from __future__ import annotations

import html as html_lib
import re
from typing import Any

BOARD_START_RE = re.compile(r'id=["\']sorftime_asinBoard_(\d+)["\']', re.I)
TAG_RE = re.compile(r'<[^>]+>', re.S)
SPACE_RE = re.compile(r'\s+')
MONTH_SALES_RE = re.compile(r'产品预计月销量\s*:\s*([0-9][0-9,]*|--)', re.I)
MONTH_REVENUE_RE = re.compile(r'产品预计月销售额\s*:\s*(\$?[0-9][0-9,.]*|--)', re.I)


def _clean_segment(segment: str) -> str:
    text = html_lib.unescape(TAG_RE.sub(' ', segment or ''))
    return SPACE_RE.sub(' ', text).strip()


def _parse_int(value: str | None):
    value = str(value or '').strip()
    if not value or value == '--':
        return ''
    try:
        return int(value.replace(',', ''))
    except Exception:
        return ''


def _parse_money(value: str | None):
    value = str(value or '').strip()
    if not value or value == '--':
        return ''
    try:
        return float(value.replace('$', '').replace(',', ''))
    except Exception:
        return ''


def _quality(row: dict[str, Any]) -> int:
    return int(row.get('sorftime_month_sales') not in ('', None)) + int(
        row.get('sorftime_month_revenue') not in ('', None)
    )


def parse_sorftime_metrics(html: str) -> dict[str, dict[str, Any]]:
    """Parse Sorftime's rendered Walmart card boards from browser HTML.

    Sorftime injects one board with an id such as ``sorftime_asinBoard_15545702811``.
    The suffix is the Walmart product ID. Values of ``--`` are preserved as an
    unavailable estimate rather than being calculated locally.
    """
    source = html or ''
    starts = list(BOARD_START_RE.finditer(source))
    out: dict[str, dict[str, Any]] = {}
    for idx, match in enumerate(starts):
        end = starts[idx + 1].start() if idx + 1 < len(starts) else len(source)
        segment = _clean_segment(source[match.start():end])
        item_id = match.group(1)
        sales_match = MONTH_SALES_RE.search(segment)
        revenue_match = MONTH_REVENUE_RE.search(segment)
        sales_raw = sales_match.group(1).strip() if sales_match else ''
        revenue_raw = revenue_match.group(1).strip() if revenue_match else ''
        row = {
            'sorftime_checked': True,
            'sorftime_month_sales': _parse_int(sales_raw),
            'sorftime_month_revenue': _parse_money(revenue_raw),
            'sorftime_month_sales_raw': sales_raw,
            'sorftime_month_revenue_raw': revenue_raw,
        }
        # Duplicate Walmart cards can appear in the rendered DOM. Keep the row
        # containing actual estimates when one duplicate is still ``--``.
        old = out.get(item_id)
        if old is None or _quality(row) > _quality(old):
            out[item_id] = row
    return out
