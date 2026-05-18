"""
GET /api/global_indicators
──────────────────────────────────────────────
홈 매크로 대시보드용. `global_indicators` 테이블에서 카테고리별로 묶어 반환.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from fastapi import APIRouter

from ..db.connections import get_stocks_conn

logger = logging.getLogger("server.routes.global_indicators")
router = APIRouter()


# 카테고리 내 심볼 표시 순서 고정 (UI 안정성)
CATEGORY_ORDER: Dict[str, List[str]] = {
    "korea":     ["KOSPI", "KOSDAQ", "KODEX200", "KRW_USD"],
    "global":    ["NASDAQ", "NASDAQ_FUT", "SP500", "SP500_FUT", "SOX", "NIKKEI", "VIX",
                  "EWY", "KORU", "DXY", "FEAR_GREED"],
    "bonds":     ["US10Y", "US2Y"],
    "commodity": ["GOLD", "SILVER", "COPPER", "WTI", "BRENT", "NATGAS"],
    "crypto":    ["BTC", "ETH", "XRP", "SOL"],
}


# 2026-05-18: server/core/numeric 로 통합 (NaN/Inf 방어 + 콤마 처리 포함 상위호환)
from server.core.numeric import to_float as _to_float


def _to_str(v):
    if v is None:
        return None
    try:
        return str(v)
    except Exception:
        return None


def _row_to_dict(row) -> Dict[str, Any]:
    symbol, display_name, category, emoji, source, source_symbol, price, change_pct, change_amt, currency, extra_json, updated_at = row
    extra = None
    if extra_json:
        try:
            extra = json.loads(extra_json) if isinstance(extra_json, str) else extra_json
        except Exception:
            extra = None
    return {
        "symbol": _to_str(symbol),
        "display_name": _to_str(display_name),
        "category": _to_str(category),
        "emoji": _to_str(emoji),
        "price": _to_float(price),
        "change_pct": _to_float(change_pct),
        "change_amt": _to_float(change_amt),
        "source": _to_str(source),
        "source_symbol": _to_str(source_symbol),
        "currency": _to_str(currency),
        "extra": extra,
        "updated_at": _to_str(updated_at),
    }


@router.get("/api/global_indicators")
def list_global_indicators():
    conn = get_stocks_conn()
    try:
        rows = conn.execute(
            """
            SELECT symbol, display_name, category, emoji, source, source_symbol,
                   price, change_pct, change_amt, currency, extra_json, updated_at
            FROM global_indicators
            """
        ).fetchall()
    finally:
        conn.close()

    by_symbol: Dict[str, Dict[str, Any]] = {}
    latest_ts: str = ""
    for r in rows:
        item = _row_to_dict(r)
        by_symbol[item["symbol"]] = item
        ts = item.get("updated_at") or ""
        if ts > latest_ts:
            latest_ts = ts

    # 카테고리별로 미리 정의된 순서로 정렬, 누락 심볼은 빈 자리(스탭)으로 건너뜀
    categories: Dict[str, List[Dict[str, Any]]] = {}
    for cat, order in CATEGORY_ORDER.items():
        bucket: List[Dict[str, Any]] = []
        for sym in order:
            item = by_symbol.get(sym)
            if item is not None:
                bucket.append(item)
        # 카탈로그에 없지만 DB엔 있는 심볼도 카테고리 뒤에 붙임 (신규 추가 즉시 노출용)
        for sym, item in by_symbol.items():
            if item.get("category") == cat and sym not in order:
                bucket.append(item)
        categories[cat] = bucket

    return {
        "categories": categories,
        "updated_at": latest_ts,
        "total": len(rows),
    }
