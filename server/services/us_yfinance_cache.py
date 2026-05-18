"""미국 종목 yfinance 스냅샷 DB 캐시.

`/api/overseas/yfinance/{symbol}` 가 페이지 진입마다 yfinance.info(1~5초)를
동기 호출하던 것을 DB hit 으로 전환. yfinance 는 cron 으로만.

스냅샷 = YFinanceCollector.get_snapshot() 전체 — quote·profile·valuation·
share_stats·analyst·holders·financials·history·news. share_stats 안에
발행주식수(shares_outstanding)·float·공매도(shares_short)·기관 보유 비율 포함.

Fallback (us_quote_cache 와 동일 패턴):
  1. DB us_yfinance_snapshot_cache (fresh, 3h 이내) → 즉시 반환
  2. DB stale → 즉시 반환 (외부 호출 X)
  3. cold (DB 기록 없음) → yfinance 1회 호출 + DB 저장

스키마: us_yfinance_snapshot_cache
  symbol TEXT PK
  snapshot_json TEXT — get_snapshot() 직렬화
  has_data BOOLEAN — yfinance 가 빈 응답이면 FALSE (재시도 폭주 방지)
  updated_at TIMESTAMP
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("server.services.us_yfinance_cache")

DB_FRESH_SEC = 3 * 3600       # 3시간 이내면 fresh
DB_STALE_OK_SEC = 7 * 86400   # 7일까지는 stale 라도 보여줌 (펀더멘털은 거의 안 변함)
TABLE = "us_yfinance_snapshot_cache"


def ensure_yf_cache_table(conn) -> None:
    """DB 캐시 테이블 — IF NOT EXISTS, 한 번만 호출하면 됨."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            symbol TEXT PRIMARY KEY,
            snapshot_json TEXT,
            has_data BOOLEAN DEFAULT TRUE,
            updated_at TIMESTAMP
        )
        """
    )
    try:
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_yf_snap_updated ON {TABLE}(updated_at DESC)")
    except Exception:
        pass
    try:
        conn.commit()
    except Exception:
        pass


def _now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_db_snapshot(conn, symbol: str) -> Optional[dict]:
    """DB 캐시 row → {snapshot, has_data, updated_at} 또는 None."""
    sym = symbol.upper().strip()
    cur = conn.execute(
        f"SELECT snapshot_json, has_data, updated_at FROM {TABLE} WHERE symbol = ?",
        (sym,),
    )
    r = cur.fetchone()
    if not r:
        return None
    try:
        snap = json.loads(r[0]) if r[0] else None
    except Exception:
        snap = None
    return {
        "snapshot": snap,
        "has_data": bool(r[1]) if r[1] is not None else (snap is not None),
        "updated_at": r[2],
    }


def upsert_db_snapshot(conn, symbol: str, snapshot: Optional[dict], has_data: bool = True) -> None:
    sym = symbol.upper().strip()
    conn.execute(
        f"""
        INSERT INTO {TABLE} (symbol, snapshot_json, has_data, updated_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (symbol) DO UPDATE SET
            snapshot_json = EXCLUDED.snapshot_json,
            has_data = EXCLUDED.has_data,
            updated_at = EXCLUDED.updated_at
        """,
        (sym, json.dumps(snapshot, default=str) if snapshot else None, has_data, _now_utc()),
    )
    try:
        conn.commit()
    except Exception:
        pass


def _snapshot_has_data(snap: Optional[dict]) -> bool:
    """yfinance 가 의미있는 데이터를 줬는지 — 빈 페니 응답 구분."""
    if not snap or not isinstance(snap, dict):
        return False
    q = snap.get("quote") or {}
    p = snap.get("profile") or {}
    ss = snap.get("share_stats") or {}
    return bool(
        q.get("price") is not None
        or p.get("long_name")
        or ss.get("shares_outstanding") is not None
    )


_YF = None


def _get_collector():
    global _YF
    if _YF is None:
        from collectors.yfinance_collector import YFinanceCollector
        _YF = YFinanceCollector()
    return _YF


def get_snapshot_cached(
    conn,
    symbol: str,
    *,
    history_period: str = "5d",
    history_interval: str = "1m",
    fresh_seconds: int = DB_FRESH_SEC,
    allow_stale: bool = True,
    force_refresh: bool = False,
) -> Optional[dict]:
    """yfinance 스냅샷 — DB(fresh) → DB(stale) → cold yfinance 호출.

    반환: snapshot dict (+ _cache 표시) 또는 None (yfinance 데이터 없음).
    force_refresh=True (cron) 면 캐시 건너뛰고 외부 호출.
    """
    sym = (symbol or "").upper().strip()
    if not sym:
        return None
    ensure_yf_cache_table(conn)
    try:
        db = get_db_snapshot(conn, sym)
    except Exception as exc:
        logger.debug("db snapshot %s: %s", sym, exc)
        db = None

    if db and db.get("updated_at") and not force_refresh:
        age = (_now_utc() - db["updated_at"]).total_seconds()
        # 1. DB fresh
        if age < fresh_seconds:
            if db.get("snapshot"):
                return {**db["snapshot"], "_cache": "db_fresh"}
            if not db.get("has_data"):
                return None  # 최근 확인했고 데이터 없음 — 재시도 안 함
        # 2. DB stale — 즉시 반환 (외부 호출 X)
        if allow_stale and age < DB_STALE_OK_SEC and db.get("snapshot"):
            return {**db["snapshot"], "_cache": "db_stale", "_stale_age_sec": int(age)}

    # 3. cold start / force_refresh — yfinance 호출
    try:
        snap = _get_collector().get_snapshot(
            sym, history_period=history_period, history_interval=history_interval,
        )
    except Exception as exc:
        logger.info("yfinance snapshot %s failed: %s", sym, exc)
        if db and db.get("snapshot"):
            return {**db["snapshot"], "_cache": "db_stale_fallback"}
        return None

    if _snapshot_has_data(snap):
        try:
            upsert_db_snapshot(conn, sym, snap, has_data=True)
        except Exception as exc:
            logger.debug("upsert snapshot %s: %s", sym, exc)
        return {**snap, "_cache": "miss"}

    # 빈 스냅샷 — has_data=False 기록 (재시도 폭주 방지), 이전 good 있으면 반환
    try:
        upsert_db_snapshot(conn, sym, None, has_data=False)
    except Exception:
        pass
    if db and db.get("snapshot"):
        return {**db["snapshot"], "_cache": "db_stale_fallback"}
    return None
