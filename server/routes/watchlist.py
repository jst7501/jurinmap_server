"""미국 페니 단타 watchlist + push trigger 등록.

스키마: us_user_watchlist (token, symbol, alert_kinds, created_at)
  alert_kinds: comma-separated subset of {halt, filing, spike}
    halt   = LULD pause 발생 시 즉시 푸시
    filing = 새 8-K / 6-K / 424B5 / S-1 등 push 대상 filing 도착 시
    spike  = pre/regular 세션 ±20% 변동 시
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException

router = APIRouter()
logger = logging.getLogger("server.routes.watchlist")

VALID_KINDS = {"halt", "filing", "spike"}


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS us_user_watchlist (
            token TEXT NOT NULL,
            symbol TEXT NOT NULL,
            alert_kinds TEXT,
            created_at TIMESTAMP,
            PRIMARY KEY (token, symbol)
        )
        """
    )
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_us_watchlist_symbol ON us_user_watchlist(symbol)")
    except Exception:
        pass
    try:
        conn.commit()
    except Exception:
        pass


def _normalize_kinds(raw) -> str:
    if not raw:
        return "halt,filing,spike"
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        items = [s.strip() for s in raw.split(",") if s.strip()]
    else:
        items = []
    kinds = sorted({k for k in items if k in VALID_KINDS})
    if not kinds:
        kinds = ["halt", "filing", "spike"]
    return ",".join(kinds)


@router.post("/api/overseas/watchlist/add")
def watchlist_add(payload: dict):
    """워치리스트 등록.
    body: {token, symbol, alert_kinds?: ["halt","filing","spike"]}
    """
    token = str((payload or {}).get("token") or "").strip()
    symbol = str((payload or {}).get("symbol") or "").strip().upper()
    if not token or len(token) < 10:
        raise HTTPException(400, "token required")
    if not symbol:
        raise HTTPException(400, "symbol required")

    kinds = _normalize_kinds((payload or {}).get("alert_kinds"))
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        _ensure_table(conn)
        conn.execute(
            """
            INSERT INTO us_user_watchlist (token, symbol, alert_kinds, created_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT(token, symbol) DO UPDATE SET
                alert_kinds = EXCLUDED.alert_kinds
            """,
            (token, symbol, kinds, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "symbol": symbol, "alert_kinds": kinds.split(",")}


@router.post("/api/overseas/watchlist/remove")
def watchlist_remove(payload: dict):
    """워치리스트 삭제. body: {token, symbol}"""
    token = str((payload or {}).get("token") or "").strip()
    symbol = str((payload or {}).get("symbol") or "").strip().upper()
    if not token or len(token) < 10:
        raise HTTPException(400, "token required")
    if not symbol:
        raise HTTPException(400, "symbol required")

    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        _ensure_table(conn)
        conn.execute("DELETE FROM us_user_watchlist WHERE token = %s AND symbol = %s", (token, symbol))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "symbol": symbol}


@router.get("/api/overseas/watchlist/list")
def watchlist_list(token: str):
    """토큰의 워치리스트 + 각 종목 메타.
    응답: {ok, items: [{symbol, name, name_ko, market_cap_usd, is_penny, alert_kinds, created_at}]}
    """
    if not token or len(token) < 10:
        raise HTTPException(400, "token required")
    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        _ensure_table(conn)
        cur = conn.execute(
            """
            SELECT w.symbol, w.alert_kinds, w.created_at,
                   us.name, us.name_ko, us.market_cap_usd, us.is_penny, us.last_price
            FROM us_user_watchlist w
            LEFT JOIN us_stocks us ON us.ticker = w.symbol
            WHERE w.token = %s
            ORDER BY w.created_at DESC
            """,
            (token,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    items = [
        {
            "symbol": r[0],
            "alert_kinds": (r[1] or "").split(",") if r[1] else [],
            "created_at": r[2].isoformat() if r[2] and hasattr(r[2], "isoformat") else r[2],
            "name": r[3],
            "name_ko": r[4],
            "market_cap_usd": float(r[5]) if r[5] is not None else None,
            "is_penny": bool(r[6]),
            "last_price": float(r[7]) if r[7] is not None else None,
        }
        for r in rows
    ]
    return {"ok": True, "items": items, "count": len(items)}


@router.get("/api/overseas/watchlist/check")
def watchlist_check(token: str, symbol: str):
    """이 종목이 워치리스트에 있는지 확인 (heart icon toggle 용)."""
    if not token or len(token) < 10:
        raise HTTPException(400, "token required")
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(400, "symbol required")
    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        _ensure_table(conn)
        cur = conn.execute(
            "SELECT alert_kinds FROM us_user_watchlist WHERE token = %s AND symbol = %s",
            (token, sym),
        )
        r = cur.fetchone()
    finally:
        conn.close()
    if not r:
        return {"ok": True, "in_watchlist": False, "alert_kinds": []}
    return {
        "ok": True,
        "in_watchlist": True,
        "alert_kinds": (r[0] or "").split(",") if r[0] else [],
    }
