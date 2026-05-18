"""미국 종목 분봉 차트 DB 캐시 (TTL 짧음, 5분).

페니 단타 진입 시각이라 캐시 TTL 매우 짧음.
페이지 진입 시 외부 호출 0회 — cron 이 5분마다 채우거나 첫 hit 시 fetch + DB 저장.

스키마: us_minute_chart_cache
  symbol+interval PK
  bars_json TEXT — [{date,time,open,high,low,close,volume}]
  count INT
  updated_at TIMESTAMP
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("server.services.us_minute_chart_cache")

DB_FRESH_MIN = 5  # 5분 fresh
TABLE = "us_minute_chart_cache"


def ensure_table(conn) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            symbol TEXT NOT NULL,
            interval_min INTEGER NOT NULL,
            bars_json TEXT,
            count INTEGER,
            updated_at TIMESTAMP,
            PRIMARY KEY (symbol, interval_min)
        )
        """
    )
    try:
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_mcc_updated ON {TABLE}(updated_at DESC)")
    except Exception:
        pass
    try:
        conn.commit()
    except Exception:
        pass


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_db(conn, symbol: str, nmin: int = 1) -> Optional[dict]:
    cur = conn.execute(
        f"SELECT bars_json, count, updated_at FROM {TABLE} WHERE symbol = ? AND interval_min = ?",
        (symbol.upper(), int(nmin)),
    )
    r = cur.fetchone()
    if not r:
        return None
    try:
        bars = json.loads(r[0]) if r[0] else []
    except Exception:
        bars = []
    return {
        "symbol": symbol.upper(),
        "interval_min": int(nmin),
        "data": bars,
        "count": int(r[1]) if r[1] is not None else len(bars),
        "updated_at": r[2],
    }


def upsert_db(conn, symbol: str, nmin: int, bars: list[dict]) -> None:
    conn.execute(
        f"""
        INSERT INTO {TABLE} (symbol, interval_min, bars_json, count, updated_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (symbol, interval_min) DO UPDATE SET
            bars_json = EXCLUDED.bars_json,
            count = EXCLUDED.count,
            updated_at = EXCLUDED.updated_at
        """,
        (symbol.upper(), int(nmin), json.dumps(bars, default=str), len(bars), _now()),
    )
    try:
        conn.commit()
    except Exception:
        pass


def get_minute_chart_cached(
    conn,
    symbol: str,
    nmin: int = 1,
    nrec: int = 240,
    *,
    force_refresh: bool = False,
    allow_stale: bool = True,
) -> Optional[dict]:
    """Fallback: DB(5min fresh) → yfinance → DB stale."""
    ensure_table(conn)
    db = get_db(conn, symbol, nmin)
    db_fresh = False
    if db and db.get("updated_at"):
        age_min = (_now() - db["updated_at"]).total_seconds() / 60
        if age_min < DB_FRESH_MIN and not force_refresh:
            db_fresh = True
    if db_fresh:
        db["_cache"] = "db_fresh"
        bars = db["data"][-int(nrec):]
        return {**db, "data": bars, "count": len(bars)}

    # DB stale 이라도 즉시 반환 — yfinance 는 cold start 시에만
    if allow_stale and db:
        db["_cache"] = "db_stale"
        bars = db["data"][-int(nrec):]
        return {**db, "data": bars, "count": len(bars)}

    # Cold start — DB 기록 전혀 없을 때만 yfinance 호출
    try:
        from collectors.yfinance_collector import YFinanceCollector
        if not hasattr(get_minute_chart_cached, "_yf"):
            get_minute_chart_cached._yf = YFinanceCollector()
        bars = get_minute_chart_cached._yf.get_minute_history(symbol, nmin=nmin, nrec=nrec)
    except Exception as exc:
        logger.debug("yfinance minute %s: %s", symbol, exc)
        bars = None

    if bars:
        upsert_db(conn, symbol, nmin, bars)
        return {
            "symbol": symbol.upper(),
            "interval_min": int(nmin),
            "data": bars,
            "count": len(bars),
            "updated_at": _now(),
            "_cache": "miss",
        }
    return None
