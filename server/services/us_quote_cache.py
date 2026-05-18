"""미국 종목 quote DB + Redis 통합 캐시.

Fallback chain (가장 빠른 것부터):
  1. Redis (60s TTL) — 동일 요청 폭주 흡수
  2. DB us_stock_quote_cache (fresh_seconds 이내) — 영구 저장
  3. Finnhub /quote (FINNHUB_API_KEY 있을 때, 광고 OK 라이선스)
  4. yfinance fast_info (마지막 fallback, rate limit 자주)
  5. 그래도 fail → 빈 응답 (last_known DB값이라도 stale 표시로 반환)

페이지 진입마다 yfinance 호출 → DB hit 으로 변경. yfinance 는 cron 으로만.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("server.services.us_quote_cache")

# Cache TTL 정책
REDIS_TTL_SEC = 60          # Redis hot cache — 1분
DB_FRESH_SEC = 600          # DB가 10분 이내면 fresh
DB_STALE_OK_SEC = 86400     # 24시간 까지는 stale 표시로라도 보여줌
QUOTE_CACHE_TABLE = "us_stock_quote_cache"


def ensure_quote_cache_table(conn) -> None:
    """DB 캐시 테이블 — 한 번만 호출하면 됨 (IF NOT EXISTS)."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {QUOTE_CACHE_TABLE} (
            symbol TEXT PRIMARY KEY,
            current_price NUMERIC(18, 6),
            prev_close NUMERIC(18, 6),
            change_amt NUMERIC(18, 6),
            change_pct NUMERIC(10, 4),
            open_price NUMERIC(18, 6),
            high NUMERIC(18, 6),
            low NUMERIC(18, 6),
            volume BIGINT,
            ask_price NUMERIC(18, 6),
            bid_price NUMERIC(18, 6),
            pre_market_price NUMERIC(18, 6),
            post_market_price NUMERIC(18, 6),
            market_cap_usd NUMERIC(20),
            source TEXT,
            updated_at TIMESTAMP
        )
        """
    )
    try:
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_quote_cache_updated ON {QUOTE_CACHE_TABLE}(updated_at DESC)")
    except Exception:
        pass
    try:
        conn.commit()
    except Exception:
        pass


def _now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _row_to_dict(row, columns: list[str]) -> Optional[dict]:
    if not row:
        return None
    return {c: row[i] for i, c in enumerate(columns)}


def _coerce_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def _coerce_int(v):
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


_COLS = [
    "symbol", "current_price", "prev_close", "change_amt", "change_pct",
    "open_price", "high", "low", "volume", "ask_price", "bid_price",
    "pre_market_price", "post_market_price", "market_cap_usd", "source", "updated_at",
]


def get_db_quote(conn, symbol: str) -> Optional[dict]:
    sym = symbol.upper().strip()
    cur = conn.execute(
        f"""
        SELECT {', '.join(_COLS)} FROM {QUOTE_CACHE_TABLE}
        WHERE symbol = ?
        """,
        (sym,),
    )
    r = cur.fetchone()
    if not r:
        return None
    d = _row_to_dict(r, _COLS)
    # numeric 타입 변환
    for k in ("current_price", "prev_close", "change_amt", "change_pct", "open_price",
              "high", "low", "ask_price", "bid_price", "pre_market_price", "post_market_price",
              "market_cap_usd"):
        d[k] = _coerce_float(d.get(k))
    d["volume"] = _coerce_int(d.get("volume"))
    if d.get("updated_at") and hasattr(d["updated_at"], "isoformat"):
        d["updated_at_iso"] = d["updated_at"].isoformat()
    return d


def upsert_db_quote(conn, symbol: str, quote: dict, source: str = "unknown") -> None:
    sym = symbol.upper().strip()
    now = _now_utc()
    conn.execute(
        f"""
        INSERT INTO {QUOTE_CACHE_TABLE} (
            symbol, current_price, prev_close, change_amt, change_pct,
            open_price, high, low, volume, ask_price, bid_price,
            pre_market_price, post_market_price, market_cap_usd,
            source, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (symbol) DO UPDATE SET
            current_price = EXCLUDED.current_price,
            prev_close = EXCLUDED.prev_close,
            change_amt = EXCLUDED.change_amt,
            change_pct = EXCLUDED.change_pct,
            open_price = COALESCE(EXCLUDED.open_price, {QUOTE_CACHE_TABLE}.open_price),
            high = COALESCE(EXCLUDED.high, {QUOTE_CACHE_TABLE}.high),
            low = COALESCE(EXCLUDED.low, {QUOTE_CACHE_TABLE}.low),
            volume = COALESCE(EXCLUDED.volume, {QUOTE_CACHE_TABLE}.volume),
            ask_price = COALESCE(EXCLUDED.ask_price, {QUOTE_CACHE_TABLE}.ask_price),
            bid_price = COALESCE(EXCLUDED.bid_price, {QUOTE_CACHE_TABLE}.bid_price),
            pre_market_price = COALESCE(EXCLUDED.pre_market_price, {QUOTE_CACHE_TABLE}.pre_market_price),
            post_market_price = COALESCE(EXCLUDED.post_market_price, {QUOTE_CACHE_TABLE}.post_market_price),
            market_cap_usd = COALESCE(EXCLUDED.market_cap_usd, {QUOTE_CACHE_TABLE}.market_cap_usd),
            source = EXCLUDED.source,
            updated_at = EXCLUDED.updated_at
        """,
        (
            sym,
            _coerce_float(quote.get("current_price")),
            _coerce_float(quote.get("prev_close")),
            _coerce_float(quote.get("change_amt")),
            _coerce_float(quote.get("change_pct")),
            _coerce_float(quote.get("open_price")),
            _coerce_float(quote.get("high")),
            _coerce_float(quote.get("low")),
            _coerce_int(quote.get("trading_volume") or quote.get("volume")),
            _coerce_float(quote.get("ask_price")),
            _coerce_float(quote.get("bid_price")),
            _coerce_float(quote.get("pre_market_price")),
            _coerce_float(quote.get("post_market_price")),
            _coerce_float(quote.get("market_cap_usd")),
            source,
            now,
        ),
    )
    try:
        conn.commit()
    except Exception:
        pass


def _fetch_from_finnhub(symbol: str) -> Optional[dict]:
    try:
        from collectors.us_finnhub import get_quote
        q = get_quote(symbol)
        if not q:
            return None
        return {
            "current_price": q.get("current_price"),
            "prev_close": q.get("prev_close"),
            "change_amt": q.get("change_amt"),
            "change_pct": q.get("change_pct"),
            "open_price": q.get("open_price"),
            "high": q.get("high"),
            "low": q.get("low"),
            "_source": "finnhub",
        }
    except Exception as exc:
        logger.debug("finnhub fetch %s: %s", symbol, exc)
        return None


def _fetch_from_yfinance(symbol: str) -> Optional[dict]:
    try:
        from collectors.yfinance_collector import YFinanceCollector
        if not hasattr(_fetch_from_yfinance, "_yf"):
            _fetch_from_yfinance._yf = YFinanceCollector()
        q = _fetch_from_yfinance._yf.get_quick_quote(symbol)
        if not q or not q.get("current_price"):
            return None
        return {
            **q,
            "_source": "yfinance",
        }
    except Exception as exc:
        logger.debug("yfinance fetch %s: %s", symbol, exc)
        return None


def get_quote_cached(
    conn,
    symbol: str,
    *,
    fresh_seconds: int = DB_FRESH_SEC,
    allow_stale: bool = True,
    force_refresh: bool = False,
) -> dict:
    """Fallback chain — Redis → DB → Finnhub → yfinance.

    fresh_seconds 이내면 DB hit 으로 즉시 반환 (외부 호출 없음).
    force_refresh=True 면 외부 API 강제 호출.
    """
    sym = symbol.upper().strip()
    if not sym:
        return {"symbol": sym, "source": "empty", "error": "no symbol"}

    # 1. Redis (1분 TTL) — 동일 요청 폭주 흡수
    try:
        from server.cache import redis_get_json
        cache_key = f"us_quote:{sym}"
        if not force_refresh:
            cached = redis_get_json(cache_key)
            if cached:
                cached["_cache"] = "redis"
                return cached
    except Exception:
        cache_key = None

    # 2. DB 캐시 (fresh)
    try:
        ensure_quote_cache_table(conn)
        db = get_db_quote(conn, sym)
    except Exception as exc:
        logger.debug("db quote %s: %s", sym, exc)
        db = None

    db_fresh = False
    if db and db.get("updated_at"):
        age_sec = (_now_utc() - db["updated_at"]).total_seconds()
        if age_sec < fresh_seconds and not force_refresh:
            db_fresh = True

    if db_fresh:
        db["_cache"] = "db_fresh"
        # Redis 도 채워주기
        try:
            from server.cache import redis_set_json
            if cache_key:
                redis_set_json(cache_key, db, REDIS_TTL_SEC)
        except Exception:
            pass
        return db

    # 3. DB stale 이라도 즉시 반환 — 외부 API 는 cold start (DB 기록 없음) 시에만
    if allow_stale and db and db.get("updated_at"):
        age_sec = (_now_utc() - db["updated_at"]).total_seconds()
        if age_sec < DB_STALE_OK_SEC:
            db["_cache"] = "db_stale"
            db["_stale_age_sec"] = int(age_sec)
            # Redis 도 채워주기
            try:
                from server.cache import redis_set_json
                if cache_key:
                    redis_set_json(cache_key, db, REDIS_TTL_SEC)
            except Exception:
                pass
            return db

    # 4. Cold start — DB 기록 없을 때만 외부 API 호출 (Finnhub → yfinance)
    fetched = _fetch_from_finnhub(sym)
    source_used = "finnhub" if fetched else None

    if not fetched:
        fetched = _fetch_from_yfinance(sym)
        source_used = "yfinance" if fetched else None

    if fetched:
        # DB 저장
        try:
            upsert_db_quote(conn, sym, fetched, source=source_used)
        except Exception as exc:
            logger.debug("upsert %s: %s", sym, exc)

        result = {
            "symbol": sym,
            "current_price": fetched.get("current_price"),
            "prev_close": fetched.get("prev_close"),
            "change_amt": fetched.get("change_amt"),
            "change_pct": fetched.get("change_pct"),
            "open_price": fetched.get("open_price"),
            "high": fetched.get("high"),
            "low": fetched.get("low"),
            "trading_volume": fetched.get("trading_volume") or fetched.get("volume"),
            "ask_price": fetched.get("ask_price"),
            "bid_price": fetched.get("bid_price"),
            "source": source_used,
            "_cache": "miss",
            "updated_at_iso": _now_utc().isoformat(),
        }
        # Redis 채우기
        try:
            from server.cache import redis_set_json
            if cache_key:
                redis_set_json(cache_key, result, REDIS_TTL_SEC)
        except Exception:
            pass
        return result

    return {"symbol": sym, "source": "none", "error": "no data available", "_cache": "fail"}
