"""미국 종목 1년 일봉 DB 캐시.

페이지 진입마다 yfinance.history(1y) 호출 → DB 캐시로 변경.
pump-dump analyzer, halt-history, 52주 차트가 공용 사용.

스키마: us_price_history_cache
  symbol TEXT PK
  period TEXT — '1y' 등
  ohlcv_json TEXT — JSON array [{date,open,high,low,close,volume}, ...]
  count INT
  updated_at TIMESTAMP

24h 이내면 DB hit, 그 외엔 yfinance 갱신 (cron 으로).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("server.services.us_price_history_cache")

DB_FRESH_HOURS = 24
TABLE = "us_price_history_cache"


def ensure_table(conn) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            symbol TEXT NOT NULL,
            period TEXT NOT NULL,
            ohlcv_json TEXT,
            count INTEGER,
            updated_at TIMESTAMP,
            PRIMARY KEY (symbol, period)
        )
        """
    )
    try:
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_phc_updated ON {TABLE}(updated_at DESC)")
    except Exception:
        pass
    try:
        conn.commit()
    except Exception:
        pass


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_db(conn, symbol: str, period: str = "1y") -> Optional[dict]:
    cur = conn.execute(
        f"SELECT ohlcv_json, count, updated_at FROM {TABLE} WHERE symbol = ? AND period = ?",
        (symbol.upper(), period),
    )
    r = cur.fetchone()
    if not r:
        return None
    try:
        ohlcv = json.loads(r[0]) if r[0] else []
    except Exception:
        ohlcv = []
    return {
        "symbol": symbol.upper(),
        "period": period,
        "data": ohlcv,
        "count": int(r[1]) if r[1] is not None else len(ohlcv),
        "updated_at": r[2],
    }


def upsert_db(conn, symbol: str, period: str, ohlcv: list[dict]) -> None:
    conn.execute(
        f"""
        INSERT INTO {TABLE} (symbol, period, ohlcv_json, count, updated_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (symbol, period) DO UPDATE SET
            ohlcv_json = EXCLUDED.ohlcv_json,
            count = EXCLUDED.count,
            updated_at = EXCLUDED.updated_at
        """,
        (symbol.upper(), period, json.dumps(ohlcv, default=str), len(ohlcv), _now()),
    )
    try:
        conn.commit()
    except Exception:
        pass


def fetch_yfinance_history(symbol: str, period: str = "1y") -> Optional[list[dict]]:
    try:
        import yfinance as yf
        hist = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=False)
        if hist is None or len(hist) == 0:
            return None
        out = []
        for idx, row in hist.iterrows():
            try:
                d = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
                out.append({
                    "date": d,
                    "open": float(row.get("Open") or 0),
                    "high": float(row.get("High") or 0),
                    "low": float(row.get("Low") or 0),
                    "close": float(row.get("Close") or 0),
                    "volume": int(row.get("Volume") or 0),
                })
            except Exception:
                continue
        return out
    except Exception as exc:
        logger.debug("yfinance history %s: %s", symbol, exc)
        return None


def get_history_cached(
    conn,
    symbol: str,
    period: str = "1y",
    *,
    force_refresh: bool = False,
    allow_stale: bool = True,
) -> Optional[dict]:
    """Fallback: DB(24h fresh) → yfinance → DB stale."""
    ensure_table(conn)
    db = get_db(conn, symbol, period)
    db_fresh = False
    if db and db.get("updated_at"):
        age_h = (_now() - db["updated_at"]).total_seconds() / 3600
        if age_h < DB_FRESH_HOURS and not force_refresh:
            db_fresh = True
    if db_fresh:
        db["_cache"] = "db_fresh"
        return db

    # DB stale 이라도 즉시 반환 — yfinance 는 cold start (DB 기록 없음) 시에만
    if allow_stale and db:
        db["_cache"] = "db_stale"
        return db

    # Cold start — DB 기록 전혀 없을 때만 yfinance 호출
    ohlcv = fetch_yfinance_history(symbol, period)
    if ohlcv:
        upsert_db(conn, symbol, period, ohlcv)
        return {
            "symbol": symbol.upper(),
            "period": period,
            "data": ohlcv,
            "count": len(ohlcv),
            "updated_at": _now(),
            "_cache": "miss",
        }
    return None
