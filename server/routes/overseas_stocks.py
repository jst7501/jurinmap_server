from __future__ import annotations

import csv
import io
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict

import requests
from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from collectors.kis_overseas_api import KISOverseasCollector
from collectors.short_data_collector import ShortDataCollector
from collectors.yfinance_collector import YFinanceCollector
from server.core.security import reject_websocket_if_unauthorized
from server.services.websocket_service import kis_ws_proxy
from utils.market_utils import get_us_market_status

logger = logging.getLogger("server.routes.overseas_stocks")

router = APIRouter(prefix="/api/overseas")
collector = KISOverseasCollector()
short_collector = ShortDataCollector()
yf_collector: YFinanceCollector | None = None

_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JurinMapBot/1.0)"}
_US_UNIVERSE_CACHE_TTL_SEC = 6 * 60 * 60
_US_QUOTES_CACHE_TTL_SEC = 90
_US_UNIVERSE_CACHE: dict[str, dict] = {}
_US_QUOTES_CACHE: dict[str, dict] = {}
_US_CACHE_LOCK = threading.Lock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# 2026-05-18: server/core/numeric 로 통합
from server.core.numeric import to_float as _safe_float


def _safe_int(value: Any) -> int:
    v = _safe_float(value)
    if v is None:
        return 0
    try:
        return int(v)
    except Exception:
        return 0


def _normalize_us_symbol(symbol: str) -> str:
    s = str(symbol or "").strip().upper()
    if not s:
        return ""
    if any(ch in s for ch in ("^", "/", "$", "=", " ")):
        return ""
    s = s.replace(".", "-")
    if len(s) > 16:
        return ""
    return s


def _parse_exchange_set(raw_exchanges: str) -> set[str]:
    mapping = {
        "NASDAQ": "NAS",
        "NAS": "NAS",
        "NYSE": "NYS",
        "NYS": "NYS",
        "AMEX": "AMS",
        "AMERICAN": "AMS",
        "AMS": "AMS",
    }
    out: set[str] = set()
    for item in str(raw_exchanges or "").split(","):
        token = mapping.get(str(item).strip().upper())
        if token:
            out.add(token)
    if not out:
        out = {"NAS", "NYS", "AMS"}
    return out


def _download_nasdaq_trader_table(url: str) -> list[dict]:
    response = requests.get(url, headers=_HTTP_HEADERS, timeout=20)
    if response.status_code != 200:
        raise RuntimeError(f"failed to fetch symbol table: {response.status_code}")

    text = response.text or ""
    reader = csv.DictReader(io.StringIO(text), delimiter="|")
    rows: list[dict] = []
    for row in reader:
        if not row:
            continue

        first_key = next(iter(row.keys()), "")
        first_val = str(row.get(first_key, "")).strip()
        if first_val.lower().startswith("file creation time"):
            break

        item: dict[str, str] = {}
        for k, v in row.items():
            if k is None:
                continue
            item[str(k).strip()] = str(v or "").strip()
        rows.append(item)

    return rows


def _fetch_us_universe_symbols(exchanges: set[str], include_etf: bool) -> list[str]:
    nasdaq_rows = _download_nasdaq_trader_table("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt")
    other_rows = _download_nasdaq_trader_table("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt")

    symbols: list[str] = []
    seen: set[str] = set()

    if "NAS" in exchanges:
        for row in nasdaq_rows:
            symbol = row.get("Symbol") or row.get("NASDAQ Symbol") or ""
            if row.get("Test Issue", "").upper() == "Y":
                continue
            if (not include_etf) and row.get("ETF", "").upper() == "Y":
                continue
            norm = _normalize_us_symbol(symbol)
            if norm and norm not in seen:
                seen.add(norm)
                symbols.append(norm)

    for row in other_rows:
        exchange_code = str(row.get("Exchange") or "").upper()
        mapped_exchange = {"N": "NYS", "A": "AMS", "P": "AMS"}.get(exchange_code)
        if mapped_exchange not in exchanges:
            continue
        if row.get("Test Issue", "").upper() == "Y":
            continue
        if (not include_etf) and row.get("ETF", "").upper() == "Y":
            continue
        symbol = row.get("ACT Symbol") or row.get("NASDAQ Symbol") or row.get("Symbol") or ""
        norm = _normalize_us_symbol(symbol)
        if norm and norm not in seen:
            seen.add(norm)
            symbols.append(norm)

    return symbols


def _get_us_universe_symbols(
    exchanges: set[str],
    include_etf: bool,
    max_universe: int,
    refresh: bool,
) -> tuple[str, list[str], dict]:
    key = f"{','.join(sorted(exchanges))}|etf={int(include_etf)}|max={max_universe}"
    now = time.time()

    with _US_CACHE_LOCK:
        cached = _US_UNIVERSE_CACHE.get(key)
        if cached and (not refresh) and (now - float(cached.get("ts") or 0) < _US_UNIVERSE_CACHE_TTL_SEC):
            cached_symbols = list(cached.get("symbols") or [])
            return key, cached_symbols, {
                "source": "cache",
                "fetched_at": cached.get("fetched_at"),
                "count": len(cached_symbols),
            }

    symbols = _fetch_us_universe_symbols(exchanges=exchanges, include_etf=include_etf)
    if max_universe > 0:
        symbols = symbols[:max_universe]

    fetched_at = _utc_now_iso()
    with _US_CACHE_LOCK:
        _US_UNIVERSE_CACHE[key] = {
            "ts": now,
            "fetched_at": fetched_at,
            "symbols": symbols,
        }

    return key, symbols, {
        "source": "remote",
        "fetched_at": fetched_at,
        "count": len(symbols),
    }


def _fetch_yahoo_quotes(symbols: list[str]) -> list[dict]:
    out: list[dict] = []
    saw_429 = False
    for i in range(0, len(symbols), 200):
        chunk = symbols[i : i + 200]
        if not chunk:
            continue

        try:
            response = requests.get(
                "https://query1.finance.yahoo.com/v7/finance/quote",
                params={"symbols": ",".join(chunk)},
                headers=_HTTP_HEADERS,
                timeout=20,
            )
            if response.status_code == 429:
                saw_429 = True
                continue
            if response.status_code != 200:
                continue
            payload = response.json() or {}
            rows = ((payload.get("quoteResponse") or {}).get("result") or [])
            if isinstance(rows, list):
                out.extend(row for row in rows if isinstance(row, dict))
        except Exception:
            continue

        time.sleep(0.03)

    if out:
        return out
    if saw_429:
        logger.warning("Yahoo quote endpoint returned HTTP 429; fallback to yfinance download")
    return _fetch_yfinance_quotes(symbols)


def _fetch_yfinance_quotes(symbols: list[str]) -> list[dict]:
    try:
        import yfinance as yf
    except Exception:
        return []

    out: list[dict] = []

    def _build_item(sym: str, row: dict) -> dict:
        return {
            "symbol": sym,
            "regularMarketPrice": row.get("Close"),
            "regularMarketVolume": row.get("Volume"),
            "regularMarketOpen": row.get("Open"),
            "regularMarketDayHigh": row.get("High"),
            "regularMarketDayLow": row.get("Low"),
            "regularMarketPreviousClose": row.get("Close"),
            "regularMarketChange": 0,
            "regularMarketChangePercent": 0,
            "marketCap": 0,
            "marketState": "",
            "exchange": "",
            "fullExchangeName": "",
            "longName": "",
            "shortName": "",
            "currency": "USD",
        }

    for i in range(0, len(symbols), 100):
        chunk = symbols[i : i + 100]
        if not chunk:
            continue
        try:
            frame = yf.download(
                tickers=" ".join(chunk),
                period="1d",
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                progress=False,
                threads=True,
            )
        except Exception:
            continue

        if frame is None or len(frame) == 0:
            continue

        try:
            cols = getattr(frame, "columns", None)
            is_multi = bool(cols is not None and getattr(cols, "nlevels", 1) > 1)
            if is_multi:
                tickers = list(dict.fromkeys(str(c).upper() for c in cols.get_level_values(0)))
                for sym in tickers:
                    if sym not in frame:
                        continue
                    sub = frame[sym]
                    if sub is None or len(sub) == 0:
                        continue
                    row = sub.tail(1).to_dict("records")
                    if not row:
                        continue
                    out.append(_build_item(sym, row[0]))
            else:
                row = frame.tail(1).to_dict("records")
                if row:
                    out.append(_build_item(str(chunk[0]).upper(), row[0]))
        except Exception:
            continue

        time.sleep(0.02)

    return out


def _get_yahoo_quotes(universe_key: str, symbols: list[str], refresh: bool) -> tuple[list[dict], dict]:
    now = time.time()

    with _US_CACHE_LOCK:
        cached = _US_QUOTES_CACHE.get(universe_key)
        if cached and (not refresh) and (now - float(cached.get("ts") or 0) < _US_QUOTES_CACHE_TTL_SEC):
            cached_quotes = list(cached.get("quotes") or [])
            return cached_quotes, {
                "source": "cache",
                "fetched_at": cached.get("fetched_at"),
                "count": len(cached_quotes),
            }

    quotes = _fetch_yahoo_quotes(symbols)
    fetched_at = _utc_now_iso()

    with _US_CACHE_LOCK:
        _US_QUOTES_CACHE[universe_key] = {
            "ts": now,
            "fetched_at": fetched_at,
            "quotes": quotes,
        }

    return quotes, {
        "source": "remote",
        "fetched_at": fetched_at,
        "count": len(quotes),
    }


def _get_yf_collector() -> YFinanceCollector:
    global yf_collector
    if yf_collector is None:
        yf_collector = YFinanceCollector()
    return yf_collector


@router.get("/price/{excd}/{symbol}")
async def get_price(
    excd: str,
    symbol: str,
    refresh_meta: bool = Query(False, description="Force refresh of short/ownership cache"),
    force_refresh: bool = Query(False, description="외부 API 강제 호출 (cron 용)"),
):
    """Quote endpoint — DB+Redis 캐시 우선 (페이지 진입 시 외부 API 호출 안 함).

    Fallback chain: Redis(60s) → DB us_stock_quote_cache(10분 fresh) → Finnhub → yfinance.
    페이지 상세 진입마다 외부 API 호출하던 패턴을 없앰 (rate limit 회피).
    cron 으로 sync_us_quote_cache.py 가 background refresh.
    """
    try:
        from server.services.us_quote_cache import get_quote_cached
        from server.db.connections import get_stocks_conn
        conn = get_stocks_conn()
        try:
            quote = get_quote_cached(conn, symbol, force_refresh=force_refresh)
        finally:
            conn.close()
        short_metrics = short_collector.get_short_metrics(symbol, force_refresh=refresh_meta)
        ownership_metrics = short_collector.get_ownership_metrics(symbol, force_refresh=refresh_meta)
        return {
            "status": "ok",
            "symbol": str(symbol).upper(),
            "exchange": str(excd).upper(),
            "market_status": get_us_market_status(),
            "quote": quote,
            "short_metrics": short_metrics,
            "ownership_metrics": ownership_metrics,
            "is_squeeze_warning": short_collector.check_squeeze_warning(symbol),
            "delay_disclaimer": "15-20분 지연 · 실시간은 본인 증권사 앱에서 확인",
            "cache_hint": quote.get("_cache"),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/chart/{excd}/{symbol}")
async def get_chart(
    excd: str,
    symbol: str,
    nmin: int = Query(1, ge=1, le=60),
    nrec: int = Query(240, ge=1, le=500),
    next_key: str = Query(""),
    fill: str = Query(""),
):
    """분봉 차트 — DB 캐시 (5min fresh) → yfinance fallback. KIS 약관 회피."""
    try:
        from server.services.us_minute_chart_cache import get_minute_chart_cached
        from server.db.connections import get_stocks_conn
        conn = get_stocks_conn()
        try:
            result = get_minute_chart_cached(conn, symbol, nmin=nmin, nrec=nrec)
        finally:
            conn.close()
        data = result.get("data") if result else []
        return {
            "status": "ok",
            "symbol": str(symbol).upper(),
            "exchange": str(excd).upper(),
            "market_status": get_us_market_status(),
            "interval_min": nmin,
            "count": len(data),
            "data": data,
            "delay_disclaimer": "15-20분 지연",
            "cache_hint": result.get("_cache") if result else None,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/short/{symbol}")
async def get_short_metrics(symbol: str, refresh: bool = Query(False)):
    try:
        data = short_collector.get_short_metrics(symbol, force_refresh=refresh)
        return {"status": "ok", "data": data}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/short/borrow-history/{symbol}")
async def get_short_borrow_history(
    symbol: str,
    days: int = Query(7, ge=1, le=90),
    refresh: bool = Query(False),
):
    try:
        data = short_collector.get_borrow_history(symbol, days=days, force_refresh=refresh)
        return {"status": "ok", "data": data}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/ownership/{symbol}")
async def get_ownership_metrics(symbol: str, refresh: bool = Query(False)):
    try:
        data = short_collector.get_ownership_metrics(symbol, force_refresh=refresh)
        return {"status": "ok", "data": data}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/snapshot/{excd}/{symbol}")
async def get_snapshot(
    excd: str,
    symbol: str,
    refresh_meta: bool = Query(False),
    include_yfinance: bool = Query(False),
):
    """Snapshot — DB+Redis 캐시 quote + short_collector meta."""
    try:
        from server.services.us_quote_cache import get_quote_cached
        from server.db.connections import get_stocks_conn
        conn = get_stocks_conn()
        try:
            quote = get_quote_cached(conn, symbol)
        finally:
            conn.close()
        meta = short_collector.get_snapshot(symbol, force_refresh=refresh_meta)
        payload: Dict[str, Any] = {
            "status": "ok",
            "symbol": str(symbol).upper(),
            "exchange": str(excd).upper(),
            "market_status": get_us_market_status(),
            "quote": quote,
            "meta": meta,
            "delay_disclaimer": "15-20분 지연",
            "cache_hint": quote.get("_cache"),
        }
        if include_yfinance:
            # 명시 요청 시에만 yfinance heavy snapshot (admin·debug 용)
            payload["yfinance"] = _get_yf_collector().get_snapshot(
                symbol,
                history_period="1d",
                history_interval="5m",
            )
        return payload
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/yfinance/{symbol}")
async def get_yfinance_snapshot(
    symbol: str,
    history_period: str = Query("5d"),
    history_interval: str = Query("1m"),
    force_refresh: bool = Query(False),
):
    """yfinance.info + 펀더멘털 스냅샷 — DB 캐시 우선 (페이지 진입 시 외부 호출 X).

    us_yfinance_snapshot_cache 에 cron(sync_us_yfinance_cache.py)이 미리 채움.
    페이지는 DB hit(수 ms). cold(DB 기록 없는 새 종목)일 때만 yfinance 1회 동기 호출.
    yfinance.info 자체가 1~5초 걸리는 무거운 호출이라 DB 캐시 필수.
    """
    sym = (symbol or "").strip().upper()
    try:
        from server.services.us_yfinance_cache import get_snapshot_cached
        from server.db.connections import get_stocks_conn
        conn = get_stocks_conn()
        try:
            data = get_snapshot_cached(
                conn, sym,
                history_period=history_period,
                history_interval=history_interval,
                force_refresh=force_refresh,
            )
        finally:
            conn.close()
    except Exception as exc:
        logger.info("yfinance snapshot %s: %s", sym, exc)
        return {"status": "ok", "data": None, "note": f"error: {exc}"}

    if data:
        return {"status": "ok", "data": data, "cache_hint": data.get("_cache")}
    return {"status": "ok", "data": None, "note": "no_yfinance_data"}


@router.get("/quotes")
async def get_quotes_bulk(symbols: str = Query(..., description="콤마 분리 심볼 리스트 (최대 200개)")):
    """여러 심볼 시세 일괄 조회 — Yahoo /v7/quote 1회 호출.

    응답 모양: `{status, count, data: [{symbol, name, price, change_pct, change_amt,
                                         prev_close, open, day_high, day_low, volume,
                                         market_cap, exchange, currency, market_state}]}`.
    호출처: 프론트 USHome 등에서 mock 정적 종목 리스트의 실시간 시세를 한 번에 채울 때.
    """
    raw = [s.strip().upper() for s in (symbols or "").split(",") if s.strip()]
    # 중복 제거 + 최대 200개
    seen: set[str] = set()
    cleaned: list[str] = []
    for s in raw:
        if s in seen:
            continue
        seen.add(s)
        cleaned.append(s)
        if len(cleaned) >= 200:
            break
    if not cleaned:
        return {"status": "ok", "count": 0, "data": []}

    try:
        rows = _fetch_yahoo_quotes(cleaned)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"quotes_fetch_failed: {exc}")

    out: list[dict] = []
    by_sym = {str(r.get("symbol") or "").upper(): r for r in rows if isinstance(r, dict)}
    for sym in cleaned:
        r = by_sym.get(sym)
        if not r:
            out.append({"symbol": sym, "price": None, "change_pct": None})
            continue
        price = _safe_float(r.get("regularMarketPrice"))
        out.append({
            "symbol": sym,
            "name": r.get("longName") or r.get("shortName") or sym,
            "exchange": r.get("fullExchangeName") or r.get("exchange") or "",
            "currency": r.get("currency") or "USD",
            "market_state": r.get("marketState") or "",
            "price": price,
            "change_pct": _safe_float(r.get("regularMarketChangePercent")),
            "change_amt": _safe_float(r.get("regularMarketChange")),
            "prev_close": _safe_float(r.get("regularMarketPreviousClose")),
            "open": _safe_float(r.get("regularMarketOpen")),
            "day_high": _safe_float(r.get("regularMarketDayHigh")),
            "day_low": _safe_float(r.get("regularMarketDayLow")),
            "volume": _safe_int(r.get("regularMarketVolume")),
            "market_cap": _safe_int(r.get("marketCap")),
            "fifty_two_week_high": _safe_float(r.get("fiftyTwoWeekHigh")),
            "fifty_two_week_low": _safe_float(r.get("fiftyTwoWeekLow")),
        })

    return {"status": "ok", "count": len(out), "data": out}


@router.get("/yfinance/options/{symbol}")
async def get_yfinance_options(
    symbol: str,
    expirations: int = Query(3, ge=1, le=10, description="가까운 만기 N개만 합산"),
):
    """옵션 P/C ratio + 평균 IV. 가까운 N개 만기만 사용 (yfinance 비용 ↑)."""
    try:
        data = _get_yf_collector().get_options_summary(symbol, max_expirations=expirations)
        return {"status": "ok", "data": data}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/yfinance/earnings/{symbol}")
async def get_yfinance_earnings(symbol: str, limit: int = Query(8, ge=1, le=20)):
    """최근 어닝 EPS 실적/추정/서프라이즈 (lastEarnings 채움)."""
    try:
        data = _get_yf_collector().get_earnings_history(symbol, limit=limit)
        return {"status": "ok", "data": data}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/yfinance/dividend/{symbol}")
async def get_yfinance_dividend(symbol: str):
    """배당률·연배당·주기·ex-date."""
    try:
        data = _get_yf_collector().get_dividend_info(symbol)
        return {"status": "ok", "data": data}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/yfinance/history/{symbol}")
async def get_yfinance_history(
    symbol: str,
    period: str = Query("5d"),
    interval: str = Query("1m"),
    prepost: bool = Query(True),
    limit: int = Query(390, ge=1, le=2000),
):
    try:
        data = _get_yf_collector().get_history(
            symbol,
            period=period,
            interval=interval,
            prepost=prepost,
            limit=limit,
        )
        return {"status": "ok", "data": data}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/screener/sub10")
async def get_sub10_screener(
    price_max: float = Query(10.0, gt=0, description="Maximum regular market price in USD"),
    price_min: float = Query(0.01, ge=0, description="Minimum regular market price in USD"),
    min_volume: int = Query(0, ge=0, description="Minimum regular market volume"),
    min_market_cap: int = Query(0, ge=0, description="Minimum market cap in USD"),
    exchanges: str = Query("NAS,NYS,AMS", description="Comma separated exchange set"),
    include_etf: bool = Query(False, description="Include ETFs in universe"),
    max_universe: int = Query(2500, ge=200, le=6000, description="Universe scan size cap"),
    limit: int = Query(100, ge=1, le=500),
    sort_by: str = Query("dollar_volume", description="dollar_volume|volume|change_pct|price"),
    refresh: bool = Query(False),
):
    try:
        ex_set = _parse_exchange_set(exchanges)
        universe_key, symbols, universe_meta = _get_us_universe_symbols(
            exchanges=ex_set,
            include_etf=include_etf,
            max_universe=max_universe,
            refresh=refresh,
        )
        quotes, quote_meta = _get_yahoo_quotes(universe_key=universe_key, symbols=symbols, refresh=refresh)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"screener_init_failed: {exc}")

    filtered = []
    for row in quotes:
        symbol = _normalize_us_symbol(row.get("symbol") or "")
        if not symbol:
            continue

        price = _safe_float(row.get("regularMarketPrice"))
        if price is None:
            continue
        if price < price_min or price > price_max:
            continue

        volume = _safe_int(row.get("regularMarketVolume"))
        if volume < min_volume:
            continue

        market_cap = _safe_int(row.get("marketCap"))
        if market_cap < min_market_cap:
            continue

        change_pct = _safe_float(row.get("regularMarketChangePercent")) or 0.0
        change_amt = _safe_float(row.get("regularMarketChange")) or 0.0
        prev_close = _safe_float(row.get("regularMarketPreviousClose"))
        open_price = _safe_float(row.get("regularMarketOpen"))
        day_high = _safe_float(row.get("regularMarketDayHigh"))
        day_low = _safe_float(row.get("regularMarketDayLow"))
        dollar_volume = float(price) * float(volume)

        filtered.append(
            {
                "symbol": symbol,
                "name": row.get("longName") or row.get("shortName") or symbol,
                "exchange": row.get("fullExchangeName") or row.get("exchange") or "",
                "market_state": row.get("marketState") or "",
                "price": round(float(price), 4),
                "change_pct": round(float(change_pct), 4),
                "change_amt": round(float(change_amt), 4),
                "prev_close": prev_close,
                "open": open_price,
                "day_high": day_high,
                "day_low": day_low,
                "volume": volume,
                "market_cap": market_cap,
                "dollar_volume": int(dollar_volume),
                "currency": row.get("currency") or "USD",
            }
        )

    sort_token = str(sort_by or "dollar_volume").strip().lower()
    if sort_token == "volume":
        filtered.sort(key=lambda x: int(x.get("volume") or 0), reverse=True)
    elif sort_token == "change_pct":
        filtered.sort(key=lambda x: float(x.get("change_pct") or 0.0), reverse=True)
    elif sort_token == "price":
        filtered.sort(key=lambda x: float(x.get("price") or 0.0))
    else:
        filtered.sort(key=lambda x: int(x.get("dollar_volume") or 0), reverse=True)

    data = filtered[:limit]
    return {
        "status": "ok",
        "filters": {
            "price_min": price_min,
            "price_max": price_max,
            "min_volume": min_volume,
            "min_market_cap": min_market_cap,
            "exchanges": sorted(ex_set),
            "include_etf": include_etf,
            "sort_by": sort_token,
            "limit": limit,
        },
        "counts": {
            "universe_symbols": len(symbols),
            "quotes_received": len(quotes),
            "matched": len(filtered),
            "returned": len(data),
        },
        "cache": {
            "universe": universe_meta,
            "quotes": quote_meta,
        },
        "market_status": get_us_market_status(),
        "fetched_at": _utc_now_iso(),
        "data": data,
    }


_US_ORDERBOOK_CACHE: dict[str, dict] = {}
_US_ORDERBOOK_TTL_SEC = 180  # yfinance 호가는 15~20분 지연 데이터 — 3분 캐시로 cold 호출(1~5초) 최소화

_US_WSB_TOP_CACHE: dict[str, dict] = {}
_US_WSB_SYM_CACHE: dict[str, dict] = {}
_US_WSB_TTL_SEC = 90  # WSB 는 90초 — Reddit rate limit 보호

_US_REDDIT_SEARCH_CACHE: dict[str, dict] = {}
_US_REDDIT_SEARCH_TTL_SEC = 90  # Reddit search 도 90초

_US_REDDIT_MENTIONS_CACHE: dict[str, dict] = {}
_US_REDDIT_MENTIONS_TTL_SEC = 120  # mentions 는 120초

_US_PENNY_CACHE: dict[str, dict] = {}
_US_PENNY_TTL_SEC = 300


@router.get("/penny/top")
async def penny_top(
    sort: str = Query("mention", description="mention / squeeze / threshold / halts / fresh"),
    limit: int = Query(30, ge=5, le=100),
    min_cap: int = Query(1_000_000, description="시총 최소 (default $1M — 미만은 0/지정폐 위험)"),
    max_cap: int = Query(100_000_000, description="시총 최대 (default $100M — 페니 cutoff)"),
):
    """페니스탁 (NASDAQ + NYSE + AMEX, non-ETF, 시총 $1M~$100M) 발견 랭킹.

    sort 옵션:
      mention   — 최근 1일 Reddit mention 합
      squeeze   — short_float_pct * days_to_cover (compute_us_squeeze_score 의 단순 버전)
      threshold — Reg SHO threshold 등재 (최근 7일)
      halts     — 최근 24h LULD halt 발생 수
      fresh     — 최근 갱신된 페니 (last_price 신선)
    """
    cache_key = f"top|{sort}|{limit}|{min_cap}|{max_cap}"
    now_ts = time.time()
    cached = _US_PENNY_CACHE.get(cache_key)
    if cached and now_ts - cached["_ts"] < _US_PENNY_TTL_SEC:
        return cached["payload"]

    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()

    # 기본 페니 universe 쿼리 — exchange 필터 + non-ETF + 시총 범위
    base_where = (
        "us.exchange IN ('NASDAQ', 'NYSE', 'NYSE_AMEX') "
        "AND (us.is_etf = FALSE OR us.is_etf IS NULL) "
        f"AND us.market_cap_usd BETWEEN {int(min_cap)} AND {int(max_cap)} "
        "AND us.market_cap_usd IS NOT NULL"
    )

    try:
        if sort == "mention":
            # 최근 Reddit __all__ snapshot 의 mention TOP + 1h 전 비교
            cur = conn.execute(f"""
                WITH latest AS (
                    SELECT MAX(snapshot_at) AS ts FROM us_reddit_mentions_snapshot WHERE subreddit = '__all__'
                ),
                cur_snap AS (
                    SELECT s.symbol, s.mention_count, s.sentiment_score
                    FROM us_reddit_mentions_snapshot s, latest
                    WHERE s.subreddit = '__all__' AND s.snapshot_at = latest.ts
                ),
                prev_snap AS (
                    SELECT DISTINCT ON (s.symbol) s.symbol, s.mention_count AS prev_mention
                    FROM us_reddit_mentions_snapshot s, latest
                    WHERE s.subreddit = '__all__'
                      AND s.snapshot_at >= latest.ts - INTERVAL '90 minutes'
                      AND s.snapshot_at <= latest.ts - INTERVAL '30 minutes'
                    ORDER BY s.symbol, s.snapshot_at DESC
                )
                SELECT us.ticker, us.name, us.name_ko, us.market_cap_usd, us.last_price, us.exchange,
                       c.mention_count, c.sentiment_score, p.prev_mention
                FROM us_stocks us
                INNER JOIN cur_snap c ON UPPER(c.symbol) = UPPER(us.ticker)
                LEFT JOIN prev_snap p ON UPPER(p.symbol) = UPPER(us.ticker)
                WHERE {base_where}
                  AND c.mention_count >= 1
                ORDER BY c.mention_count DESC NULLS LAST
                LIMIT {int(limit)}
            """)
        elif sort == "short_volume":
            # FINRA daily short volume — 최근 5영업일 평균 ratio 높은 페니
            cur = conn.execute(f"""
                SELECT us.ticker, us.name, us.name_ko, us.market_cap_usd, us.last_price, us.exchange,
                       AVG(sv.short_volume_ratio) AS avg_sv_ratio,
                       SUM(sv.total_volume) AS total_vol,
                       MAX(sv.trade_date) AS last_date
                FROM us_stocks us
                INNER JOIN us_short_volume_daily sv ON UPPER(sv.symbol) = UPPER(us.ticker)
                WHERE {base_where}
                  AND sv.trade_date >= (CURRENT_DATE - INTERVAL '5 days')::text
                  AND sv.short_volume_ratio IS NOT NULL
                  AND sv.total_volume >= 50000
                GROUP BY us.ticker, us.name, us.name_ko, us.market_cap_usd, us.last_price, us.exchange
                HAVING COUNT(*) >= 1
                ORDER BY avg_sv_ratio DESC
                LIMIT {int(limit)}
            """)
        elif sort == "threshold":
            cur = conn.execute(f"""
                SELECT us.ticker, us.name, us.name_ko, us.market_cap_usd, us.last_price, us.exchange,
                       t.as_of_date, t.market_category
                FROM us_stocks us
                INNER JOIN (
                    SELECT DISTINCT ON (symbol) symbol, as_of_date, market_category
                    FROM us_threshold_securities_daily
                    WHERE as_of_date >= (CURRENT_DATE - INTERVAL '7 days')::text
                    ORDER BY symbol, as_of_date DESC
                ) t ON UPPER(t.symbol) = UPPER(us.ticker)
                WHERE {base_where}
                ORDER BY us.market_cap_usd ASC
                LIMIT {int(limit)}
            """)
        elif sort == "halts":
            cur = conn.execute(f"""
                SELECT us.ticker, us.name, us.name_ko, us.market_cap_usd, us.last_price, us.exchange,
                       COUNT(*) as halt_count
                FROM us_stocks us
                INNER JOIN us_short_interest_daily si ON UPPER(si.symbol) = UPPER(us.ticker)
                WHERE {base_where}
                  AND si.as_of_date >= (CURRENT_DATE - INTERVAL '14 days')::text
                GROUP BY us.ticker, us.name, us.name_ko, us.market_cap_usd, us.last_price, us.exchange
                ORDER BY halt_count DESC
                LIMIT {int(limit)}
            """)
        elif sort == "squeeze":
            cur = conn.execute(f"""
                SELECT us.ticker, us.name, us.name_ko, us.market_cap_usd, us.last_price, us.exchange,
                       si.short_float_pct, si.days_to_cover
                FROM us_stocks us
                INNER JOIN (
                    SELECT DISTINCT ON (symbol) symbol, short_float_pct, days_to_cover
                    FROM us_short_interest_daily
                    ORDER BY symbol, as_of_date DESC
                ) si ON UPPER(si.symbol) = UPPER(us.ticker)
                WHERE {base_where}
                  AND si.short_float_pct > 10
                ORDER BY (si.short_float_pct * COALESCE(si.days_to_cover, 1)) DESC NULLS LAST
                LIMIT {int(limit)}
            """)
        else:  # fresh
            cur = conn.execute(f"""
                SELECT us.ticker, us.name, us.name_ko, us.market_cap_usd, us.last_price, us.exchange,
                       us.market_cap_updated_at
                FROM us_stocks us
                WHERE {base_where}
                ORDER BY us.market_cap_updated_at DESC NULLS LAST
                LIMIT {int(limit)}
            """)

        rows = cur.fetchall()
        col_names_map = {
            "mention": ["ticker", "name", "name_ko", "market_cap_usd", "last_price", "exchange",
                        "mention_count", "sentiment_score", "prev_mention"],
            "short_volume": ["ticker", "name", "name_ko", "market_cap_usd", "last_price", "exchange",
                             "avg_sv_ratio", "total_volume", "last_trade_date"],
            "threshold": ["ticker", "name", "name_ko", "market_cap_usd", "last_price", "exchange",
                          "as_of_date", "market_category"],
            "halts": ["ticker", "name", "name_ko", "market_cap_usd", "last_price", "exchange", "halt_count"],
            "squeeze": ["ticker", "name", "name_ko", "market_cap_usd", "last_price", "exchange",
                        "short_float_pct", "days_to_cover"],
            "fresh": ["ticker", "name", "name_ko", "market_cap_usd", "last_price", "exchange",
                      "market_cap_updated_at"],
        }
        cols = col_names_map.get(sort, col_names_map["fresh"])

        data = []
        for r in rows:
            d = {}
            for i, name in enumerate(cols):
                val = r[i]
                if name in ("market_cap_usd", "last_price", "short_float_pct", "days_to_cover",
                            "avg_sv_ratio"):
                    d[name] = float(val) if val is not None else None
                elif name == "market_cap_updated_at" and val is not None and hasattr(val, "isoformat"):
                    d[name] = val.isoformat()
                else:
                    d[name] = val
            # mention 정렬일 때 1h delta 계산
            if sort == "mention":
                cur_m = d.get("mention_count") or 0
                prev_m = d.get("prev_mention")
                if prev_m is not None and prev_m > 0:
                    d["delta_1h_pct"] = round((cur_m - prev_m) / prev_m * 100, 1)
                    d["delta_1h_abs"] = cur_m - prev_m
                else:
                    d["delta_1h_pct"] = None
                    d["delta_1h_abs"] = None
            data.append(d)
    finally:
        conn.close()

    payload = {
        "status": "ok",
        "sort": sort,
        "min_cap": int(min_cap),
        "max_cap": int(max_cap),
        "count": len(data),
        "data": data,
        "fetched_at": _utc_now_iso(),
    }
    _US_PENNY_CACHE[cache_key] = {"_ts": now_ts, "payload": payload}
    return payload


@router.get("/stocks")
async def stocks_list(
    q: str = Query("", description="검색어 (ticker / 영문명 / 한글명)"),
    exchange: str = Query("", description="콤마 구분 (예: NASDAQ,NYSE,NYSE_AMEX)"),
    is_penny: str = Query("", description="페니 필터: true / false / '' (전체)"),
    exclude_etf: bool = Query(False, description="ETF 제외"),
    sort: str = Query("cap_desc", description="cap_desc / cap_asc / ticker / price_desc / price_asc / recent"),
    page: int = Query(1, ge=1, le=200),
    limit: int = Query(50, ge=10, le=100),
):
    """us_stocks DB 직접 검색 — yfinance 호출 0회.

    검색 ranking:
      1. ticker exact match
      2. ticker prefix
      3. name ILIKE %q%
      4. name_ko ILIKE %q% (한글 검색 지원)
    """
    where_parts: list[str] = []
    params: list = []

    # 검색어
    q_clean = (q or "").strip()
    if q_clean:
        q_upper = q_clean.upper()
        like_q = f"%{q_clean}%"
        where_parts.append("(UPPER(ticker) = ? OR UPPER(ticker) LIKE ? OR name ILIKE ? OR name_ko ILIKE ?)")
        params.extend([q_upper, f"{q_upper}%", like_q, like_q])

    # 거래소
    if exchange:
        ex_list = [e.strip() for e in exchange.split(",") if e.strip()]
        if ex_list:
            placeholders = ",".join(["?"] * len(ex_list))
            where_parts.append(f"exchange IN ({placeholders})")
            params.extend(ex_list)

    # 페니 필터
    if is_penny.lower() == "true":
        where_parts.append("is_penny = TRUE")
    elif is_penny.lower() == "false":
        where_parts.append("(is_penny = FALSE OR is_penny IS NULL)")

    # ETF 필터
    if exclude_etf:
        where_parts.append("(is_etf = FALSE OR is_etf IS NULL)")

    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

    # 정렬 — 검색 매칭일 때 ranking 우선 (parameterized — psycopg % 충돌 차단)
    order_params: list = []
    if q_clean:
        q_upper = q_clean.upper()
        order_sql = (
            "ORDER BY "
            "  CASE WHEN UPPER(ticker) = ? THEN 0 "
            "       WHEN UPPER(ticker) LIKE ? THEN 1 "
            "       ELSE 2 END, "
            "  market_cap_usd DESC NULLS LAST"
        )
        order_params.extend([q_upper, f"{q_upper}%"])
    elif sort == "cap_desc":
        order_sql = "ORDER BY market_cap_usd DESC NULLS LAST"
    elif sort == "cap_asc":
        order_sql = "ORDER BY market_cap_usd ASC NULLS LAST"
    elif sort == "value_desc":
        # 거래대금 (10일 평균 거래량 × 가격) 내림차순 — 페니 활성 종목 우선
        order_sql = "ORDER BY (COALESCE(avg_volume_10d, 0) * COALESCE(last_price, 0)) DESC NULLS LAST"
    elif sort == "volume_desc":
        order_sql = "ORDER BY avg_volume_10d DESC NULLS LAST"
    elif sort == "ticker":
        order_sql = "ORDER BY ticker ASC"
    elif sort == "price_desc":
        order_sql = "ORDER BY last_price DESC NULLS LAST"
    elif sort == "price_asc":
        order_sql = "ORDER BY last_price ASC NULLS LAST"
    elif sort == "recent":
        order_sql = "ORDER BY market_cap_updated_at DESC NULLS LAST"
    else:
        order_sql = "ORDER BY market_cap_usd DESC NULLS LAST"

    offset = (int(page) - 1) * int(limit)

    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        # total count
        cur = conn.execute(f"SELECT COUNT(*) FROM us_stocks{where_sql}", params)
        total = int(cur.fetchone()[0] or 0)

        cur = conn.execute(
            f"""
            SELECT ticker, name, name_ko, exchange, market_category,
                   market_cap_usd, last_price, is_penny, is_etf,
                   sector, market_cap_updated_at,
                   avg_volume_10d
            FROM us_stocks
            {where_sql}
            {order_sql}
            LIMIT {int(limit)} OFFSET {offset}
            """,
            params + order_params,
        )
        rows = cur.fetchall()
        data = []
        for r in rows:
            avg_vol_10d = int(r[11]) if r[11] is not None else None
            last_price_val = float(r[6]) if r[6] is not None else None
            dollar_volume = None
            if avg_vol_10d and last_price_val:
                dollar_volume = int(avg_vol_10d * last_price_val)
            data.append({
                "ticker": r[0],
                "name": r[1],
                "name_ko": r[2],
                "exchange": r[3],
                "market_category": r[4],
                "avg_volume_10d": avg_vol_10d,
                "dollar_volume_10d_usd": dollar_volume,
                "market_cap_usd": float(r[5]) if r[5] is not None else None,
                "last_price": float(r[6]) if r[6] is not None else None,
                "is_penny": bool(r[7]) if r[7] is not None else None,
                "is_etf": bool(r[8]) if r[8] is not None else None,
                "sector": r[9],
                "market_cap_updated_at": r[10].isoformat() if r[10] and hasattr(r[10], "isoformat") else r[10],
            })
    finally:
        conn.close()

    return {
        "status": "ok",
        "query": q_clean,
        "filters": {
            "exchange": exchange or None,
            "is_penny": is_penny or None,
            "exclude_etf": exclude_etf,
        },
        "page": int(page),
        "limit": int(limit),
        "total": total,
        "has_more": offset + len(data) < total,
        "count": len(data),
        "data": data,
        "fetched_at": _utc_now_iso(),
    }


@router.get("/stocks/{symbol}")
async def stocks_detail(symbol: str):
    """단일 ticker 의 DB-only 정보 — yfinance 호출 X.

    회사 정보 (summary/industry/CEO/...) + 거래량 + 시총 + 한글명 모두 DB 에서.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")
    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        # us_stock_quote_cache 와 LEFT JOIN — prev_close/change_pct 같이 (stage 1 에서 즉시 변동률 표시)
        cur = conn.execute(
            """
            SELECT
                us.ticker, us.name, us.name_ko, us.exchange, us.market_category,
                us.market_cap_usd, us.last_price, us.is_penny, us.is_etf,
                us.sector, us.industry, us.market_cap_updated_at,
                us.summary, us.sector_full, us.employees, us.website,
                us.country, us.state, us.city, us.hq_address, us.ceo_name, us.ceo_title,
                us.shares_outstanding, us.float_shares,
                us.insider_pct, us.institutional_pct,
                us.avg_volume_10d, us.avg_volume_3m,
                us.fifty_two_week_high, us.fifty_two_week_low, us.beta,
                us.trailing_pe, us.forward_pe, us.price_to_book, us.dividend_yield_pct,
                us.company_info_updated_at,
                us.sec_cik, us.sec_name, us.sic_code, us.sic_description,
                us.state_of_incorporation, us.fiscal_year_end, us.filer_category,
                us.ein, us.phone, us.business_address, us.investor_website,
                us.former_names_json, us.sec_exchanges_json, us.sec_meta_updated_at,
                q.current_price, q.prev_close, q.change_amt, q.change_pct, q.volume, q.updated_at
            FROM us_stocks us
            LEFT JOIN us_stock_quote_cache q ON q.symbol = us.ticker
            WHERE us.ticker = ?
            """,
            (sym,),
        )
        r = cur.fetchone()
        if not r:
            return {"status": "not_found", "symbol": sym, "data": None}

        def _num(v):
            return float(v) if v is not None else None
        def _int(v):
            return int(v) if v is not None else None
        def _iso(v):
            return v.isoformat() if v and hasattr(v, "isoformat") else v

        data = {
            "ticker": r[0],
            "name": r[1],
            "name_ko": r[2],
            "exchange": r[3],
            "market_category": r[4],
            "market_cap_usd": _num(r[5]),
            "last_price": _num(r[6]),
            "is_penny": bool(r[7]) if r[7] is not None else None,
            "is_etf": bool(r[8]) if r[8] is not None else None,
            "sector": r[9],
            "industry": r[10] or r[13],   # us_stocks.industry > sector_full (yfinance industry)
            "market_cap_updated_at": _iso(r[11]),
            # 회사 정보
            "summary": r[12],
            "sector_full": r[13],
            "employees": _int(r[14]),
            "website": r[15],
            "country": r[16],
            "state": r[17],
            "city": r[18],
            "hq_address": r[19],
            "ceo_name": r[20],
            "ceo_title": r[21],
            # 주식 구조
            "shares_outstanding": _int(r[22]),
            "float_shares": _int(r[23]),
            "insider_pct": _num(r[24]),
            "institutional_pct": _num(r[25]),
            # 거래량 / 변동성
            "avg_volume_10d": _int(r[26]),
            "avg_volume_3m": _int(r[27]),
            "fifty_two_week_high": _num(r[28]),
            "fifty_two_week_low": _num(r[29]),
            "beta": _num(r[30]),
            # 밸류에이션
            "trailing_pe": _num(r[31]),
            "forward_pe": _num(r[32]),
            "price_to_book": _num(r[33]),
            "dividend_yield_pct": _num(r[34]),
            "company_info_updated_at": _iso(r[35]),
            # SEC EDGAR 메타 (보강)
            "sec_cik": r[36],
            "sec_name": r[37],
            "sic_code": r[38],
            "sic_description": r[39],
            "state_of_incorporation": r[40],
            "fiscal_year_end": r[41],
            "filer_category": r[42],
            "ein": r[43],
            "phone": r[44],
            "business_address": r[45],
            "investor_website": r[46],
            "former_names": (lambda v: __import__("json").loads(v) if v else [])(r[47]),
            "sec_exchanges": (lambda v: __import__("json").loads(v) if v else [])(r[48]),
            "sec_meta_updated_at": _iso(r[49]),
            # quote_cache 에서 보강 (있을 때) — stage 1 에서 즉시 변동률 표시
            "quote_current_price": _num(r[50]),
            "quote_prev_close": _num(r[51]),
            "quote_change_amt": _num(r[52]),
            "quote_change_pct": _num(r[53]),
            "quote_volume": _int(r[54]),
            "quote_updated_at": _iso(r[55]),
        }
        # quote_cache 의 current_price 가 있으면 last_price 갱신
        if data.get("quote_current_price") and data.get("quote_current_price") > 0:
            data["last_price"] = data["quote_current_price"]
        # 손바꿈 회전율 계산 — avg_volume_10d / float_shares
        if data["avg_volume_10d"] and data["float_shares"]:
            data["turnover_ratio"] = round(data["avg_volume_10d"] / data["float_shares"], 4)
        else:
            data["turnover_ratio"] = None

        return {"status": "ok", "data": data}
    finally:
        conn.close()


@router.get("/stocks/{symbol}/short")
async def stocks_short_metrics(symbol: str):
    """공매도 종합 — 모든 소스 통합 (SI/CTB/FTD/Threshold/FINRA Volume).

    페니에 필요한 모든 공매도 지표 한 번에:
      - SI 현재 + 이전월 + MoM 변화율 (yfinance + stockanalysis)
      - DTC, % of float
      - Borrow fee (iBorrowDesk)
      - Available shares (iBorrowDesk)
      - FINRA short volume (최근 5일 평균)
      - Threshold securities 등재 여부
      - FTD 누적 (60일)
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")

    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        # 1. SI + 이전월 + MoM (us_short_interest_daily 최신)
        cur = conn.execute(
            """
            SELECT short_interest_shares, short_float_pct, days_to_cover,
                   shares_short_prior_month, short_change_mom_pct,
                   date_short_interest, date_short_prior, net_borrowing,
                   data_source, as_of_date
            FROM us_short_interest_daily
            WHERE symbol = ?
            ORDER BY as_of_date DESC LIMIT 1
            """,
            (sym,),
        )
        si_row = cur.fetchone()
        si = None
        if si_row:
            si = {
                "shares_short": int(si_row[0]) if si_row[0] is not None else None,
                "short_float_pct": float(si_row[1]) if si_row[1] is not None else None,
                "days_to_cover": float(si_row[2]) if si_row[2] is not None else None,
                "shares_short_prior_month": int(si_row[3]) if si_row[3] is not None else None,
                "short_change_mom_pct": float(si_row[4]) if si_row[4] is not None else None,
                "date_short_interest": si_row[5].isoformat() if si_row[5] and hasattr(si_row[5], "isoformat") else si_row[5],
                "date_short_prior": si_row[6].isoformat() if si_row[6] and hasattr(si_row[6], "isoformat") else si_row[6],
                "net_borrowing": float(si_row[7]) if si_row[7] is not None else None,
                "source": si_row[8],
                "as_of_date": si_row[9],
            }

        # 2. Borrow (iBorrowDesk 최신)
        cur = conn.execute(
            """
            SELECT borrow_fee_pct, available_shares, rebate_rate_pct, as_of_date
            FROM us_short_borrow_daily
            WHERE symbol = ?
            ORDER BY as_of_date DESC LIMIT 1
            """,
            (sym,),
        )
        b_row = cur.fetchone()
        borrow = None
        if b_row:
            borrow = {
                "borrow_fee_pct": float(b_row[0]) if b_row[0] is not None else None,
                "available_shares": int(b_row[1]) if b_row[1] is not None else None,
                "rebate_rate_pct": float(b_row[2]) if b_row[2] is not None else None,
                "as_of_date": b_row[3],
            }

        # 3. FINRA short volume 최근 5일 평균
        cur = conn.execute(
            """
            SELECT AVG(short_volume_ratio), SUM(total_volume), COUNT(*), MAX(trade_date)
            FROM us_short_volume_daily
            WHERE symbol = ?
              AND trade_date >= (CURRENT_DATE - INTERVAL '7 days')::text
            """,
            (sym,),
        )
        fv_row = cur.fetchone()
        finra = None
        if fv_row and fv_row[0] is not None:
            finra = {
                "avg_short_volume_ratio_5d": float(fv_row[0]),
                "total_volume_5d": int(fv_row[1]) if fv_row[1] is not None else None,
                "days_count": int(fv_row[2]) if fv_row[2] is not None else 0,
                "last_trade_date": fv_row[3],
            }

        # 4. Threshold (NYSE/NASDAQ 등재)
        cur = conn.execute(
            """
            SELECT as_of_date, market FROM us_threshold_securities_daily
            WHERE symbol = ?
              AND as_of_date >= (CURRENT_DATE - INTERVAL '14 days')::text
            ORDER BY as_of_date DESC LIMIT 1
            """,
            (sym,),
        )
        t_row = cur.fetchone()
        threshold = None
        if t_row:
            threshold = {"as_of_date": t_row[0], "market": t_row[1]}

        # 5. FTD 60일 누적
        cur = conn.execute(
            """
            SELECT SUM(fail_quantity), COUNT(*), MAX(settlement_date)
            FROM us_ftd_daily
            WHERE symbol = ?
              AND settlement_date >= (CURRENT_DATE - INTERVAL '60 days')::text
              AND fail_quantity > 0
            """,
            (sym,),
        )
        ftd_row = cur.fetchone()
        ftd = None
        if ftd_row and ftd_row[0] is not None:
            ftd = {
                "total_fails_60d": int(ftd_row[0]),
                "days_with_fails": int(ftd_row[1]) if ftd_row[1] is not None else 0,
                "last_settlement_date": ftd_row[2],
            }

        # 6. us_stocks (float, shares_outstanding)
        cur = conn.execute(
            "SELECT float_shares, shares_outstanding, is_penny FROM us_stocks WHERE ticker = ?",
            (sym,),
        )
        st_row = cur.fetchone()
        stock_meta = {
            "float_shares": int(st_row[0]) if st_row and st_row[0] is not None else None,
            "shares_outstanding": int(st_row[1]) if st_row and st_row[1] is not None else None,
            "is_penny": bool(st_row[2]) if st_row and st_row[2] is not None else None,
        } if st_row else {}
    finally:
        conn.close()

    # Utilization 계산 (대여 사용률)
    utilization_pct = None
    if borrow and borrow.get("available_shares") and stock_meta.get("float_shares") and stock_meta["float_shares"] > 0:
        avail = borrow["available_shares"]
        flt = stock_meta["float_shares"]
        utilization_pct = round(max(0, min(100, (1 - avail / flt) * 100)), 2)

    return {
        "status": "ok",
        "symbol": sym,
        "short_interest": si,
        "borrow": borrow,
        "finra_volume": finra,
        "threshold": threshold,
        "ftd": ftd,
        "stock_meta": stock_meta,
        "utilization_pct": utilization_pct,
        "fetched_at": _utc_now_iso(),
    }


@router.get("/stocks/{symbol}/insider-trades")
async def stocks_insider_trades(symbol: str, days: int = Query(180, ge=30, le=730), limit: int = Query(100, ge=10, le=500)):
    """OpenInsider 내부자 거래 — us_insider_trades 테이블."""
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")
    days_int = max(30, min(730, int(days)))
    limit_int = max(10, min(500, int(limit)))

    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        cur = conn.execute(
            f"""
            SELECT filing_date, trade_date, insider_name, title, trade_type, trade_type_raw,
                   price, qty, owned_after, delta_own_pct, value
            FROM us_insider_trades
            WHERE symbol = ?
              AND trade_date >= (CURRENT_DATE - INTERVAL '{days_int} days')::date
            ORDER BY trade_date DESC NULLS LAST, filing_date DESC
            LIMIT {limit_int}
            """,
            (sym,),
        )
        rows = cur.fetchall()
        data = []
        for r in rows:
            data.append({
                "filing_date": r[0].isoformat() if r[0] and hasattr(r[0], "isoformat") else r[0],
                "trade_date": r[1].isoformat() if r[1] and hasattr(r[1], "isoformat") else r[1],
                "insider_name": r[2],
                "title": r[3],
                "trade_type": r[4],
                "trade_type_raw": r[5],
                "price": float(r[6]) if r[6] is not None else None,
                "qty": float(r[7]) if r[7] is not None else None,
                "owned_after": float(r[8]) if r[8] is not None else None,
                "delta_own_pct": float(r[9]) if r[9] is not None else None,
                "value": float(r[10]) if r[10] is not None else None,
            })

        # 30일 cluster 요약
        from datetime import date as _date, timedelta as _td
        cutoff = (_date.today() - _td(days=30)).isoformat()
        recent = [t for t in data if (t.get("trade_date") or "") >= cutoff]
        p_30d = [t for t in recent if t["trade_type"] == "P"]
        s_30d = [t for t in recent if t["trade_type"] == "S"]
        p_insiders = len({t["insider_name"] for t in p_30d if t.get("insider_name")})
        s_insiders = len({t["insider_name"] for t in s_30d if t.get("insider_name")})
        summary = {
            "p_30d_count": len(p_30d),
            "s_30d_count": len(s_30d),
            "p_30d_insiders": p_insiders,
            "s_30d_insiders": s_insiders,
            "p_30d_value": int(sum((t.get("value") or 0) for t in p_30d)),
            "s_30d_value": int(sum(abs(t.get("value") or 0) for t in s_30d)),
            "cluster_buy": p_insiders >= 3,
            "cluster_sell": s_insiders >= 3,
        }
    finally:
        conn.close()
    return {"status": "ok", "symbol": sym, "count": len(data), "summary": summary, "data": data, "fetched_at": _utc_now_iso()}


@router.get("/stocks/{symbol}/financials")
async def stocks_financials(symbol: str):
    """SEC Company Facts — 분기/연 재무 + cash runway."""
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")
    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        cur = conn.execute(
            """
            SELECT symbol, cik, entity_name,
                   revenue_usd, revenue_end, revenue_form,
                   net_income_usd, net_income_end, net_income_form,
                   cash_usd, cash_end, cash_form,
                   op_cash_usd, op_cash_end, op_cash_form,
                   assets_usd, liabilities_usd, equity_usd, shares_outstanding,
                   burn_monthly_usd, cash_runway_months,
                   revenue_series_json, net_income_series_json,
                   cash_series_json, op_cash_series_json,
                   updated_at
            FROM us_sec_financials WHERE symbol = ?
            """,
            (sym,),
        )
        r = cur.fetchone()
    finally:
        conn.close()
    if not r:
        return {"status": "ok", "symbol": sym, "data": None}

    import json as _json
    def _f(v): return float(v) if v is not None else None
    def _iso(v): return v.isoformat() if v and hasattr(v, "isoformat") else v
    def _series(s):
        try:
            return _json.loads(s) if s else []
        except Exception:
            return []

    data = {
        "symbol": r[0], "cik": r[1], "entity_name": r[2],
        "revenue": {"value": _f(r[3]), "end": _iso(r[4]), "form": r[5]},
        "net_income": {"value": _f(r[6]), "end": _iso(r[7]), "form": r[8]},
        "cash": {"value": _f(r[9]), "end": _iso(r[10]), "form": r[11]},
        "op_cash": {"value": _f(r[12]), "end": _iso(r[13]), "form": r[14]},
        "assets": _f(r[15]),
        "liabilities": _f(r[16]),
        "equity": _f(r[17]),
        "shares_outstanding": _f(r[18]),
        "burn_monthly_usd": _f(r[19]),
        "cash_runway_months": _f(r[20]),
        "revenue_series": _series(r[21]),
        "net_income_series": _series(r[22]),
        "cash_series": _series(r[23]),
        "op_cash_series": _series(r[24]),
        "updated_at": _iso(r[25]),
    }
    return {"status": "ok", "symbol": sym, "data": data, "fetched_at": _utc_now_iso()}


@router.get("/stocks/{symbol}/filings")
async def stocks_filings(
    symbol: str,
    limit: int = Query(50, ge=5, le=200),
    forms: str = Query("", description="comma-separated form filter (e.g. 8-K,S-1,424B5)"),
    dilution_only: bool = Query(False),
    summary_only: bool = Query(False, description="요약 대상 (8-K/6-K) 만"),
):
    """SEC EDGAR submissions — 최근 N건 filings + AI 한국어 요약(있을 때).

    page 의 dilution risk · 8-K AI 요약 위젯 공용 입력.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")
    from server.db.connections import get_stocks_conn
    import json as _json
    conn = get_stocks_conn()
    try:
        where = "WHERE sf.symbol = ?"
        params: list = [sym]
        if dilution_only:
            where += " AND sf.is_dilution = TRUE"
        if summary_only:
            where += " AND sf.is_summary_target = TRUE"
        if forms:
            form_list = [f.strip() for f in forms.split(",") if f.strip()]
            if form_list:
                placeholders = ",".join(["?"] * len(form_list))
                where += f" AND sf.form IN ({placeholders})"
                params.extend(form_list)
        cur = conn.execute(
            f"""
            SELECT sf.accession, sf.cik, sf.form, sf.filing_date, sf.report_date,
                   sf.primary_doc, sf.primary_doc_desc, sf.items, sf.size_bytes,
                   sf.doc_url, sf.is_dilution, sf.is_summary_target,
                   fs.one_liner, fs.drivers_json, fs.tone, fs.status, fs.updated_at
            FROM us_sec_filings sf
            LEFT JOIN us_filing_summaries fs ON fs.symbol = sf.symbol AND fs.accession = sf.accession
            {where}
            ORDER BY sf.filing_date DESC, sf.accession DESC
            LIMIT {int(limit)}
            """,
            params,
        )
        rows = cur.fetchall()
        data = []
        for r in rows:
            drivers = []
            if r[13]:
                try:
                    drivers = _json.loads(r[13])
                except Exception:
                    drivers = []
            data.append({
                "accession": r[0],
                "cik": r[1],
                "form": r[2],
                "filing_date": r[3].isoformat() if r[3] and hasattr(r[3], "isoformat") else r[3],
                "report_date": r[4].isoformat() if r[4] and hasattr(r[4], "isoformat") else r[4],
                "primary_doc": r[5],
                "primary_doc_desc": r[6],
                "items": r[7],
                "size_bytes": int(r[8]) if r[8] is not None else 0,
                "doc_url": r[9],
                "is_dilution": bool(r[10]),
                "is_summary_target": bool(r[11]),
                "summary": {
                    "one_liner": r[12],
                    "drivers": drivers,
                    "tone": r[14],
                    "status": r[15],
                    "updated_at": r[16].isoformat() if r[16] and hasattr(r[16], "isoformat") else r[16],
                } if r[12] else None,
            })
    finally:
        conn.close()
    return {"status": "ok", "symbol": sym, "count": len(data), "data": data, "fetched_at": _utc_now_iso()}


@router.get("/stocks/{symbol}/dilution-risk")
async def stocks_dilution_risk(symbol: str, window_days: int = Query(180, ge=30, le=720)):
    """페니 dilution risk score — us_sec_filings DB 집계 + subtype 분류.

    score 가중치:
      424B5/424B4: 3.0  S-1/F-1: 2.0  S-3/F-3: 1.5
    subtype 위험 가산 (severity):
      ATM / Reverse Split / Going Concern / Delisting: +2.0 추가
      PIPE / Registered Direct: +1.5
      Warrant / Convertible: +1.0
    tier: score>=8 심각 / 5-8 주의 / 2-5 관찰 / 0-2 깨끗
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")
    from server.db.connections import get_stocks_conn
    from datetime import datetime, timedelta
    from collectors.us_sec_filings import DILUTION_WEIGHTS, DILUTION_SUBTYPES

    # subtype 별 가산점
    SUBTYPE_BONUS = {
        "atm": 2.0,
        "reverse_split": 2.0,
        "going_concern": 2.0,
        "delisting_risk": 2.0,
        "pipe": 1.5,
        "registered_direct": 1.5,
        "warrant": 1.0,
        "convertible": 1.0,
        "shelf": 0.0,
        "general_offering": 0.0,
    }

    cutoff = (datetime.utcnow() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    conn = get_stocks_conn()
    try:
        # is_dilution + subtype 으로 reverse_split/going_concern/delisting 도 포함 (8-K 도 비-dilution 이지만 위험)
        cur = conn.execute(
            """
            SELECT form, filing_date, accession, primary_doc, primary_doc_desc, doc_url, items, subtype, is_dilution
            FROM us_sec_filings
            WHERE symbol = ? AND filing_date >= ?
              AND (is_dilution = TRUE OR subtype IN ('reverse_split','going_concern','delisting_risk'))
            ORDER BY filing_date DESC
            """,
            (sym, cutoff),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    score = 0.0
    counts: dict[str, int] = {}
    subtype_counts: dict[str, int] = {}
    latest_offering = None
    latest_critical = None  # ATM / Reverse Split / Going Concern / Delisting 중 최신
    filings_list = []
    for r in rows:
        form = r[0]
        fd = r[1].isoformat() if r[1] and hasattr(r[1], "isoformat") else r[1]
        subtype = r[7]
        is_dilution = bool(r[8])
        w_form = DILUTION_WEIGHTS.get(form, 0) if is_dilution else 0
        w_subtype = SUBTYPE_BONUS.get(subtype or "", 0)
        weight = w_form + w_subtype
        score += weight
        counts[form] = counts.get(form, 0) + 1
        if subtype:
            subtype_counts[subtype] = subtype_counts.get(subtype, 0) + 1
        subtype_meta = DILUTION_SUBTYPES.get(subtype or "", None)
        entry = {
            "form": form,
            "filing_date": fd,
            "accession": r[2],
            "primary_doc": r[3],
            "primary_doc_desc": r[4],
            "doc_url": r[5],
            "items": r[6],
            "subtype": subtype,
            "subtype_label": subtype_meta[0] if subtype_meta else None,
            "subtype_color": subtype_meta[1] if subtype_meta else None,
            "subtype_desc": subtype_meta[2] if subtype_meta else None,
            "weight": round(weight, 1),
        }
        filings_list.append(entry)
        if form in ("424B5", "424B4", "424B2", "S-1", "F-1") and (not latest_offering or fd > (latest_offering.get("filing_date") or "")):
            latest_offering = entry
        if subtype in ("atm", "reverse_split", "going_concern", "delisting_risk", "pipe") and (not latest_critical or fd > (latest_critical.get("filing_date") or "")):
            latest_critical = entry

    if score >= 8:
        tier, tier_color = "심각", "red"
    elif score >= 5:
        tier, tier_color = "주의", "orange"
    elif score >= 2:
        tier, tier_color = "관찰", "yellow"
    else:
        tier, tier_color = "깨끗", "gray"

    return {
        "status": "ok",
        "symbol": sym,
        "data": {
            "score": round(score, 1),
            "tier": tier,
            "tier_color": tier_color,
            "window_days": window_days,
            "counts": counts,
            "subtype_counts": subtype_counts,
            "subtype_meta": {k: {"label": v[0], "color": v[1], "desc": v[2]} for k, v in DILUTION_SUBTYPES.items()},
            "total_dilution_filings": sum(counts.values()),
            "latest_offering": latest_offering,
            "latest_critical": latest_critical,
            "filings": filings_list,
        },
        "fetched_at": _utc_now_iso(),
    }


@router.get("/stocks/{symbol}/news")
async def stocks_news(symbol: str, limit: int = Query(30, ge=5, le=100), source: str = Query("")):
    """종목 뉴스 + SEC 공시 — us_company_news 테이블."""
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")
    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        where = "WHERE symbol = ?"
        params = [sym]
        if source:
            where += " AND source = ?"
            params.append(source)
        cur = conn.execute(
            f"""
            SELECT source, url, title, form_type, publisher, summary, published_at
            FROM us_company_news
            {where}
            ORDER BY published_at DESC NULLS LAST
            LIMIT {int(limit)}
            """,
            params,
        )
        rows = cur.fetchall()
        data = [
            {
                "source": r[0],
                "url": r[1],
                "title": r[2],
                "form_type": r[3],
                "publisher": r[4],
                "summary": r[5],
                "published_at": r[6].isoformat() if r[6] and hasattr(r[6], "isoformat") else r[6],
            }
            for r in rows
        ]
    finally:
        conn.close()
    return {"status": "ok", "symbol": sym, "count": len(data), "data": data, "fetched_at": _utc_now_iso()}


@router.get("/stocks/{symbol}/officers")
async def stocks_officers(symbol: str):
    """종목 임원 리스트 — us_stocks.officers_json."""
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")
    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        cur = conn.execute("SELECT officers_json FROM us_stocks WHERE ticker = ?", (sym,))
        r = cur.fetchone()
    finally:
        conn.close()
    if not r or not r[0]:
        return {"status": "ok", "symbol": sym, "count": 0, "data": []}
    import json as _json
    try:
        officers = _json.loads(r[0])
    except Exception:
        officers = []
    return {"status": "ok", "symbol": sym, "count": len(officers), "data": officers}


@router.get("/stocks/{symbol}/price-history")
async def stocks_price_history(symbol: str, period: str = Query("1y", description="1mo / 3mo / 6mo / 1y / 2y")):
    """일봉 OHLCV — DB 캐시 (24h fresh) → yfinance fallback. 52주 sparkline 용."""
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")

    # mem cache 10분 (server local) — DB hit 도 줄임
    cache_key = f"price_history|{sym}|{period}"
    now_ts = time.time()
    if not hasattr(stocks_price_history, "_cache"):
        stocks_price_history._cache = {}
    cached = stocks_price_history._cache.get(cache_key)
    if cached and now_ts - cached["_ts"] < 600:
        return cached["payload"]

    # DB 캐시 (24h fresh) → yfinance fallback
    from server.services.us_price_history_cache import get_history_cached
    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        result = get_history_cached(conn, sym, period=period)
    finally:
        conn.close()

    if not result or not result.get("data"):
        payload = {"status": "ok", "symbol": sym, "period": period, "count": 0, "data": []}
        stocks_price_history._cache[cache_key] = {"_ts": now_ts, "payload": payload}
        return payload

    # frontend 가 사용하는 shape 으로 변환 (date/close/high/low/volume)
    data = [
        {"date": r["date"], "close": r.get("close"), "high": r.get("high"),
         "low": r.get("low"), "volume": r.get("volume")}
        for r in result["data"] if r.get("close") is not None
    ]
    payload = {
        "status": "ok", "symbol": sym, "period": period,
        "count": len(data), "data": data,
        "cache_hint": result.get("_cache"),
        "fetched_at": _utc_now_iso(),
    }
    stocks_price_history._cache[cache_key] = {"_ts": now_ts, "payload": payload}
    return payload


@router.get("/stocks/{symbol}/turnover-history")
async def stocks_turnover_history(symbol: str, days: int = Query(30, ge=7, le=90)):
    """일별 회전율 — daily volume / float_shares.

    페니 펌프 진행 추적 — 회전율 spike 가 가장 강한 시그널.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")

    # 1. float_shares 가져옴 (us_stocks)
    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        cur = conn.execute("SELECT float_shares, shares_outstanding FROM us_stocks WHERE ticker = ?", (sym,))
        r = cur.fetchone()
    finally:
        conn.close()
    if not r or (not r[0] and not r[1]):
        return {"status": "ok", "symbol": sym, "count": 0, "data": [], "note": "no float data"}

    base = int(r[0] or r[1] or 0)
    if base <= 0:
        return {"status": "ok", "symbol": sym, "count": 0, "data": [], "note": "no float data"}

    # 2. daily volume (yfinance.history)
    period = "1mo" if days <= 30 else "3mo"
    try:
        import yfinance as yf
        hist = yf.Ticker(sym).history(period=period, interval="1d", auto_adjust=False)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"yfinance history failed: {exc}")

    if hist is None or len(hist) == 0:
        return {"status": "ok", "symbol": sym, "count": 0, "data": []}

    data = []
    for idx, row in hist.iterrows():
        try:
            vol = int(row.get("Volume")) if row.get("Volume") is not None else None
            if not vol or vol <= 0:
                continue
            date_str = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
            ratio = vol / base
            data.append({
                "date": date_str,
                "volume": vol,
                "turnover_ratio": round(ratio, 4),
            })
        except Exception:
            continue
    data = data[-int(days):]

    return {
        "status": "ok", "symbol": sym, "days": int(days),
        "float_base": base,
        "count": len(data), "data": data,
        "fetched_at": _utc_now_iso(),
    }


@router.get("/penny/halts")
async def penny_halts(
    hours: int = Query(24, ge=1, le=72),
    active_only: bool = Query(False),
):
    """페니 universe 안에서 발생한 LULD halt — 실시간 발견.

    NASDAQ Trader RSS 의 halt 를 us_stocks (페니 필터) 와 JOIN.
    반환: ticker + 한글명 + 시총 + halt 정보 (reason, expected_resume).
    """
    try:
        from collectors.us_trade_halts import get_recent_halts
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"halts import failed: {exc}")

    try:
        all_halts = get_recent_halts(active_only=active_only, hours=int(hours), max_items=200)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    if not all_halts:
        return {"status": "ok", "count": 0, "data": [], "fetched_at": _utc_now_iso()}

    # 페니 universe + 한글명 결합
    symbols = list({h.get("symbol", "").upper() for h in all_halts if h.get("symbol")})
    if not symbols:
        return {"status": "ok", "count": 0, "data": [], "fetched_at": _utc_now_iso()}

    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        placeholders = ",".join(["?"] * len(symbols))
        cur = conn.execute(
            f"""
            SELECT ticker, name, name_ko, market_cap_usd, last_price, exchange, is_penny
            FROM us_stocks
            WHERE UPPER(ticker) IN ({placeholders})
              AND is_penny = TRUE
              AND (is_etf = FALSE OR is_etf IS NULL)
              AND exchange IN ('NASDAQ','NYSE','NYSE_AMEX')
            """,
            symbols,
        )
        penny_meta = {
            r[0].upper(): {
                "name": r[1], "name_ko": r[2],
                "market_cap_usd": float(r[3]) if r[3] is not None else None,
                "last_price": float(r[4]) if r[4] is not None else None,
                "exchange": r[5],
            }
            for r in cur.fetchall()
        }
    finally:
        conn.close()

    # 페니 universe 안의 halt 만 통과
    out = []
    for h in all_halts:
        sym = h.get("symbol", "").upper()
        meta = penny_meta.get(sym)
        if not meta:
            continue
        out.append({**h, **meta})

    # halt 시각 내림차순
    out.sort(key=lambda x: x.get("halt_at_utc") or "", reverse=True)

    return {
        "status": "ok",
        "count": len(out),
        "data": out,
        "fetched_at": _utc_now_iso(),
        "source": "nasdaq_trader_rss + us_stocks penny filter",
    }


@router.get("/penny/short-volume")
async def penny_short_volume(
    days: int = Query(5, ge=1, le=30, description="최근 N영업일 누적"),
    limit: int = Query(30, ge=5, le=100),
    min_volume: int = Query(50_000, description="총 거래량 minimum (noise 필터)"),
):
    """FINRA Reg SHO Daily Short Volume × 페니 universe 융합.

    페니 종목 중 최근 N일 short_volume_ratio (공매도 비중) 평균 높은 순.
    short_volume_ratio = 일별 공매도 거래량 / 총 거래량.
    """
    days_int = max(1, min(30, int(days)))
    limit_int = max(5, min(100, int(limit)))
    min_vol_int = max(0, int(min_volume))

    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        cur = conn.execute(
            f"""
            SELECT us.ticker, us.name, us.name_ko, us.market_cap_usd, us.last_price, us.exchange,
                   AVG(sv.short_volume_ratio) AS avg_sv_ratio,
                   SUM(sv.total_volume) AS total_vol,
                   COUNT(*) AS days_count,
                   MAX(sv.trade_date) AS last_date
            FROM us_stocks us
            INNER JOIN us_short_volume_daily sv ON UPPER(sv.symbol) = UPPER(us.ticker)
            WHERE us.is_penny = TRUE
              AND (us.is_etf = FALSE OR us.is_etf IS NULL)
              AND us.exchange IN ('NASDAQ','NYSE','NYSE_AMEX')
              AND sv.trade_date >= (CURRENT_DATE - INTERVAL '{days_int} days')::text
              AND sv.short_volume_ratio IS NOT NULL
              AND sv.total_volume >= {min_vol_int}
            GROUP BY us.ticker, us.name, us.name_ko, us.market_cap_usd, us.last_price, us.exchange
            HAVING COUNT(*) >= 1
            ORDER BY avg_sv_ratio DESC
            LIMIT {limit_int}
            """,
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    data = [
        {
            "ticker": r[0],
            "name": r[1],
            "name_ko": r[2],
            "market_cap_usd": float(r[3]) if r[3] is not None else None,
            "last_price": float(r[4]) if r[4] is not None else None,
            "exchange": r[5],
            "avg_sv_ratio": float(r[6]) if r[6] is not None else None,
            "total_volume": int(r[7]) if r[7] is not None else None,
            "days_count": int(r[8]),
            "last_trade_date": r[9],
        }
        for r in rows
    ]

    return {
        "status": "ok",
        "lookback_days": days_int,
        "count": len(data),
        "data": data,
        "fetched_at": _utc_now_iso(),
        "source": "finra_cnms × penny universe",
    }


@router.get("/penny/short-volume/{symbol}")
async def penny_short_volume_series(
    symbol: str,
    days: int = Query(30, ge=7, le=90),
):
    """단일 페니 종목의 FINRA daily short volume 시계열 — sparkline 차트용."""
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")
    days_int = max(7, min(90, int(days)))

    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        cur = conn.execute(
            f"""
            SELECT trade_date, short_volume, total_volume, short_volume_ratio
            FROM us_short_volume_daily
            WHERE symbol = ?
              AND trade_date >= (CURRENT_DATE - INTERVAL '{days_int} days')::text
            ORDER BY trade_date ASC
            """,
            (sym,),
        )
        rows = cur.fetchall()
        data = [
            {
                "trade_date": r[0],
                "short_volume": int(r[1]) if r[1] is not None else None,
                "total_volume": int(r[2]) if r[2] is not None else None,
                "short_volume_ratio": float(r[3]) if r[3] is not None else None,
            }
            for r in rows
        ]
    finally:
        conn.close()

    return {
        "status": "ok",
        "symbol": sym,
        "days": days_int,
        "count": len(data),
        "data": data,
        "fetched_at": _utc_now_iso(),
    }


@router.get("/penny/pulse")
async def penny_pulse(hours: int = Query(2, ge=1, le=24), limit: int = Query(20, ge=5, le=50)):
    """페니 통합 이벤트 stream — halt / dilution / spike / news 시간순.

    각 이벤트: {ts, kind, symbol, name_ko, title, tone, url, sub}
    """
    from server.db.connections import get_stocks_conn
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(tzinfo=None)
    events = []
    conn = get_stocks_conn()
    try:
        # 1. halt
        cur = conn.execute(
            """
            SELECT h.symbol, h.halt_at_utc, h.reason_code, h.reason_kr, h.halt_type,
                   us.name_ko, us.name
            FROM us_halt_events h
            LEFT JOIN us_stocks us ON us.ticker = h.symbol
            WHERE h.halt_at_utc >= ?
              AND us.is_penny = TRUE
            ORDER BY h.halt_at_utc DESC LIMIT ?
            """,
            (cutoff, limit),
        )
        for r in cur.fetchall():
            ts = r[1].isoformat() if r[1] and hasattr(r[1], "isoformat") else r[1]
            events.append({
                "ts": ts,
                "kind": "halt",
                "symbol": r[0],
                "name_ko": r[5],
                "name": r[6],
                "title": f"{r[0]} 거래정지 — {r[3] or r[2]}",
                "sub": r[4] or r[2],
                "tone": "red",
                "url": f"/us/stock/NAS/{r[0]}",
            })

        # 2. dilution filings 새로 등장
        cur = conn.execute(
            """
            SELECT sf.symbol, sf.filing_date, sf.form, sf.subtype, sf.primary_doc_desc,
                   sf.doc_url, us.name_ko, us.name
            FROM us_sec_filings sf
            INNER JOIN us_stocks us ON us.ticker = sf.symbol
            WHERE sf.filing_date >= ?::date
              AND sf.is_dilution = TRUE
              AND us.is_penny = TRUE
            ORDER BY sf.filing_date DESC, sf.accession DESC LIMIT ?
            """,
            (cutoff, limit),
        )
        from collectors.us_sec_filings import DILUTION_SUBTYPES
        for r in cur.fetchall():
            ts = r[1].isoformat() if r[1] and hasattr(r[1], "isoformat") else r[1]
            subtype_meta = DILUTION_SUBTYPES.get(r[3] or "", None)
            sub_label = subtype_meta[0] if subtype_meta else r[2]
            events.append({
                "ts": ts,
                "kind": "dilution",
                "symbol": r[0],
                "name_ko": r[6],
                "name": r[7],
                "title": f"{r[0]} {sub_label} 발견",
                "sub": (r[4] or "")[:60],
                "tone": "orange" if r[3] in ("warrant", "convertible", "general_offering") else "red",
                "url": r[5] or f"/us/stock/NAS/{r[0]}",
            })

        # 3. quote spike — us_stock_quote_cache 의 |change_pct| >= 20
        cur = conn.execute(
            """
            SELECT q.symbol, q.updated_at, q.change_pct, q.current_price,
                   us.name_ko, us.name
            FROM us_stock_quote_cache q
            INNER JOIN us_stocks us ON us.ticker = q.symbol
            WHERE q.updated_at >= ?
              AND us.is_penny = TRUE
              AND ABS(q.change_pct) >= 20
            ORDER BY q.updated_at DESC LIMIT ?
            """,
            (cutoff, limit),
        )
        for r in cur.fetchall():
            ts = r[1].isoformat() if r[1] and hasattr(r[1], "isoformat") else r[1]
            pct = float(r[2]) if r[2] is not None else 0
            sign = "+" if pct > 0 else ""
            events.append({
                "ts": ts,
                "kind": "spike",
                "symbol": r[0],
                "name_ko": r[4],
                "name": r[5],
                "title": f"{r[0]} {sign}{pct:.1f}% 급{'등' if pct > 0 else '락'}",
                "sub": f"${float(r[3] or 0):.4f}",
                "tone": "red" if pct > 0 else "blue",
                "url": f"/us/stock/NAS/{r[0]}",
            })
    finally:
        conn.close()

    # 시간 내림차순 + 중복 제거 (같은 symbol+kind+같은 분)
    events.sort(key=lambda e: e["ts"] or "", reverse=True)
    seen = set()
    deduped = []
    for e in events:
        key = (e["symbol"], e["kind"], (e["ts"] or "")[:16])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    return {
        "status": "ok",
        "hours": hours,
        "events": deduped[:limit],
        "count": len(deduped),
        "fetched_at": _utc_now_iso(),
    }


@router.get("/halts/stats")
async def halts_stats(hours: int = Query(24, ge=1, le=720)):
    """LULD halt 누적 통계 — 상시 감시 카드 (홈) 용.

    각 query try/except 로 graceful — 일부 fail 시 빈 값 반환.
    """
    from server.db.connections import get_stocks_conn
    from datetime import datetime, timedelta, timezone
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now_utc - timedelta(hours=hours)

    # 활성 halt — RSS 라이브 (DB 누적 외)
    active_live = []
    try:
        from collectors.us_trade_halts import get_recent_halts
        active_live = get_recent_halts(active_only=True, hours=2, max_items=50) or []
    except Exception as exc:
        logger.debug("active halts fetch: %s", exc)

    # 방금 재개됨 — 최근 10분 내 expected_resume 이 지난 halt
    just_resumed = []
    try:
        from collectors.us_trade_halts import get_recent_halts as _grh
        recent_all = _grh(active_only=False, hours=1, max_items=80) or []
        for r in recent_all:
            try:
                resume_iso = r.get("expected_resume_at_utc")
                if not resume_iso:
                    continue
                resume_dt = datetime.fromisoformat(resume_iso.replace("Z", "+00:00"))
                if resume_dt.tzinfo is None:
                    resume_dt = resume_dt.replace(tzinfo=timezone.utc)
                delta = (now_utc.replace(tzinfo=timezone.utc) - resume_dt).total_seconds()
                if 0 <= delta <= 600:  # 0~10분 전 재개
                    just_resumed.append(r)
            except Exception:
                continue
        just_resumed.sort(key=lambda x: x.get("expected_resume_at_utc") or "", reverse=True)
        just_resumed = just_resumed[:5]
    except Exception as exc:
        logger.debug("just-resumed halts fetch: %s", exc)

    window_count = 0
    window_symbols = 0
    window_pennies = 0
    reason_dist = []
    top_symbols = []
    latest = []

    conn = get_stocks_conn()
    try:
        # us_halt_events 테이블 존재 확인 (없으면 빈 응답)
        try:
            cur = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT symbol) FROM us_halt_events WHERE halt_at_utc >= %s",
                (cutoff,),
            )
            r = cur.fetchone()
            window_count = int(r[0] or 0)
            window_symbols = int(r[1] or 0)
        except Exception as exc:
            logger.debug("halts window query: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass

        # 페니 발생 수
        try:
            cur = conn.execute(
                """
                SELECT COUNT(*) FROM us_halt_events h
                INNER JOIN us_stocks us ON us.ticker = h.symbol
                WHERE h.halt_at_utc >= %s AND us.is_penny = TRUE
                """,
                (cutoff,),
            )
            window_pennies = int(cur.fetchone()[0] or 0)
        except Exception as exc:
            logger.debug("halts penny query: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass

        # 사유 분포
        try:
            cur = conn.execute(
                """
                SELECT reason_code, halt_type, COUNT(*) FROM us_halt_events
                WHERE halt_at_utc >= %s
                GROUP BY reason_code, halt_type
                ORDER BY 3 DESC LIMIT 10
                """,
                (cutoff,),
            )
            reason_dist = [
                {"reason_code": r[0], "halt_type": r[1], "count": int(r[2] or 0)}
                for r in cur.fetchall()
            ]
        except Exception as exc:
            logger.debug("halts reason query: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass

        # top 종목
        try:
            cur = conn.execute(
                """
                SELECT h.symbol, MAX(us.name_ko), MAX(us.name), BOOL_OR(us.is_penny), COUNT(*) AS cnt
                FROM us_halt_events h
                LEFT JOIN us_stocks us ON us.ticker = h.symbol
                WHERE h.halt_at_utc >= %s
                GROUP BY h.symbol
                ORDER BY cnt DESC LIMIT 5
                """,
                (cutoff,),
            )
            top_symbols = [
                {
                    "symbol": r[0],
                    "name_ko": r[1],
                    "name": r[2],
                    "is_penny": bool(r[3]) if r[3] is not None else False,
                    "count": int(r[4] or 0),
                }
                for r in cur.fetchall()
            ]
        except Exception as exc:
            logger.debug("halts top query: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass

        # 최근 5건 + 같은 종목 24h 연속 횟수 (window function)
        try:
            cur = conn.execute(
                """
                WITH recent AS (
                    SELECT h.symbol, h.halt_at_utc, h.reason_code, h.reason_kr, h.halt_type,
                           h.expected_resume_at_utc, us.name_ko, us.name, us.is_penny,
                           COUNT(*) OVER (PARTITION BY h.symbol) AS sym_count
                    FROM us_halt_events h
                    LEFT JOIN us_stocks us ON us.ticker = h.symbol
                    WHERE h.halt_at_utc >= %s
                )
                SELECT * FROM recent
                ORDER BY halt_at_utc DESC LIMIT 8
                """,
                (cutoff,),
            )
            latest = [
                {
                    "symbol": r[0],
                    "halt_at_utc": r[1].isoformat() if r[1] and hasattr(r[1], "isoformat") else r[1],
                    "reason_code": r[2],
                    "reason_kr": r[3],
                    "halt_type": r[4],
                    "expected_resume_at_utc": r[5].isoformat() if r[5] and hasattr(r[5], "isoformat") else r[5],
                    "name_ko": r[6],
                    "name": r[7],
                    "is_penny": bool(r[8]) if r[8] is not None else False,
                    "consecutive_count": int(r[9] or 1),  # 같은 종목 24h 누적 정지 횟수
                }
                for r in cur.fetchall()
            ]
        except Exception as exc:
            logger.debug("halts latest query: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # 같은 종목 24h 누적 정지 횟수·시간 — 5분킷 → 10분킷 연장 표시용
    # us_halt_events 의 last 24h 데이터로 cumulative_min 계산
    halt_cum_map: dict[str, dict] = {}
    try:
        cutoff_24h = (now_utc - timedelta(hours=24))
        conn_c = get_stocks_conn()
        try:
            cur = conn_c.execute(
                """
                SELECT symbol, COUNT(*) AS cnt, MIN(halt_at_utc) AS first_halt,
                       SUM(EXTRACT(EPOCH FROM (COALESCE(expected_resume_at_utc, halt_at_utc + INTERVAL '5 minutes') - halt_at_utc))) AS total_sec
                FROM us_halt_events
                WHERE halt_at_utc >= %s
                GROUP BY symbol
                """,
                (cutoff_24h,),
            )
            for r in cur.fetchall():
                halt_cum_map[r[0]] = {
                    "halts_24h": int(r[1] or 1),
                    "first_halt_at": r[2].isoformat() if r[2] and hasattr(r[2], "isoformat") else None,
                    "cumulative_paused_sec": int(r[3] or 0),
                }
        finally:
            try:
                conn_c.close()
            except Exception:
                pass
    except Exception as exc:
        logger.debug("halt cumulative query: %s", exc)

    # active + just_resumed 종목 가격 enrich — us_stock_quote_cache LEFT JOIN
    enrich_syms = list({(h.get("symbol") or "").upper() for h in (active_live[:5] + just_resumed) if h.get("symbol")})
    quote_map: dict[str, dict] = {}
    if enrich_syms:
        conn2 = get_stocks_conn()
        try:
            cur = conn2.execute(
                """
                SELECT ticker, current_price, prev_close, change_amt, change_pct, volume_today, is_penny
                FROM us_stock_quote_cache q
                LEFT JOIN us_stocks us ON us.ticker = q.ticker
                WHERE q.ticker = ANY(%s)
                """,
                (enrich_syms,),
            )
            for r in cur.fetchall():
                quote_map[r[0]] = {
                    "last_price": float(r[1]) if r[1] is not None else None,
                    "prev_close": float(r[2]) if r[2] is not None else None,
                    "change_amt": float(r[3]) if r[3] is not None else None,
                    "change_pct": float(r[4]) if r[4] is not None else None,
                    "volume_today": int(r[5]) if r[5] is not None else None,
                    "is_penny": bool(r[6]) if r[6] is not None else None,
                }
        except Exception as exc:
            logger.debug("halt quote enrich: %s", exc)
            try:
                conn2.rollback()
            except Exception:
                pass
        finally:
            try:
                conn2.close()
            except Exception:
                pass

    def _enrich(h: dict) -> dict:
        sym = (h.get("symbol") or "").upper()
        q = quote_map.get(sym) or {}
        out = dict(h)
        out["last_price"] = q.get("last_price")
        out["change_pct"] = q.get("change_pct")
        out["volume_today"] = q.get("volume_today")
        if "is_penny" not in out or out.get("is_penny") is None:
            out["is_penny"] = q.get("is_penny") or False

        # 상킷·하킷 구분 — change_pct 양수=상킷, 음수=하킷, 0/None=불명
        cp = out.get("change_pct")
        if cp is not None:
            out["direction"] = "up" if cp > 0 else ("down" if cp < 0 else "flat")
            out["direction_kr"] = "상킷" if cp > 0 else ("하킷" if cp < 0 else "보합킷")
        else:
            out["direction"] = "unknown"
            out["direction_kr"] = "방향 미확정"

        # 누적 정지 시간 (24h 안 같은 종목 LUDP+LUDS 합산)
        cum = halt_cum_map.get(sym) or {}
        out["halts_24h"] = cum.get("halts_24h", 1)
        out["cumulative_paused_sec"] = cum.get("cumulative_paused_sec", 300)
        # 5분킷 → 10분킷 escalation phase
        cum_min = (cum.get("cumulative_paused_sec") or 300) // 60
        if cum_min <= 5:
            out["escalation_phase"] = "5min"
            out["escalation_label"] = "5분킷"
        elif cum_min <= 10:
            out["escalation_phase"] = "10min"
            out["escalation_label"] = "5분킷 → 10분킷 연장"
        elif cum_min <= 30:
            out["escalation_phase"] = "30min"
            out["escalation_label"] = f"누적 {cum_min}분 정지"
        else:
            out["escalation_phase"] = "long"
            out["escalation_label"] = f"누적 {cum_min}분 — 장기 정지"

        # 재정지 예측 — 변동률 폭·페니·연속 횟수·거래량 기반 (호가 데이터 없이 통계 추정)
        risk = 0
        if cp is not None:
            if abs(cp) >= 30: risk += 40
            elif abs(cp) >= 15: risk += 25
            elif abs(cp) >= 10: risk += 12
        if out.get("is_penny"):
            risk += 15
        consec = out.get("consecutive_count") or out.get("halts_24h") or 1
        if consec >= 3: risk += 30
        elif consec >= 2: risk += 18
        vol = out.get("volume_today") or 0
        # 단순 임계 — 페니는 100만+ 거래량 시 가산
        if out.get("is_penny") and vol >= 1_000_000:
            risk += 10
        risk = min(95, risk)
        out["rehalt_risk"] = risk
        if risk >= 60:
            out["rehalt_label"] = "재정지 위험 높음"
        elif risk >= 35:
            out["rehalt_label"] = "재정지 가능"
        else:
            out["rehalt_label"] = None
        return out

    enriched_active = [_enrich(h) for h in active_live[:5]]
    enriched_resumed = [_enrich(h) for h in just_resumed]

    return {
        "status": "ok",
        "active_count": len(active_live),
        "active_halts": enriched_active,
        "just_resumed": enriched_resumed,
        "window_hours": hours,
        "window_count": window_count,
        "window_symbols": window_symbols,
        "window_pennies": window_pennies,
        "reason_dist": reason_dist,
        "top_symbols": top_symbols,
        "latest": latest,
        "fetched_at": _utc_now_iso(),
    }


@router.get("/penny/summary")
async def penny_summary():
    """페니 universe 통계 — universe 크기, sub 분포, 최근 발견."""
    cache_key = "summary"
    now_ts = time.time()
    cached = _US_PENNY_CACHE.get(cache_key)
    if cached and now_ts - cached["_ts"] < _US_PENNY_TTL_SEC:
        return cached["payload"]

    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        # 페니 universe — 거래소별 분포
        cur = conn.execute("""
            SELECT exchange, COUNT(*) FROM us_stocks
            WHERE is_penny = TRUE
              AND (is_etf = FALSE OR is_etf IS NULL)
              AND market_cap_usd >= 1000000
            GROUP BY exchange ORDER BY 2 DESC
        """)
        by_exchange = [{"exchange": r[0], "count": r[1]} for r in cur.fetchall()]
        total = sum(x["count"] for x in by_exchange)

        # 시총 buckets
        cur = conn.execute("""
            SELECT
              SUM(CASE WHEN market_cap_usd < 10000000 THEN 1 ELSE 0 END) AS u10m,
              SUM(CASE WHEN market_cap_usd >= 10000000 AND market_cap_usd < 30000000 THEN 1 ELSE 0 END) AS m10_30,
              SUM(CASE WHEN market_cap_usd >= 30000000 AND market_cap_usd < 100000000 THEN 1 ELSE 0 END) AS m30_100,
              COUNT(*) AS total
            FROM us_stocks
            WHERE is_penny = TRUE
              AND (is_etf = FALSE OR is_etf IS NULL)
              AND exchange IN ('NASDAQ', 'NYSE', 'NYSE_AMEX')
        """)
        r = cur.fetchone()
        buckets = {
            "under_10m": int(r[0] or 0),
            "10m_to_30m": int(r[1] or 0),
            "30m_to_100m": int(r[2] or 0),
            "total_penny": int(r[3] or 0),
        }
    finally:
        conn.close()

    payload = {
        "status": "ok",
        "by_exchange": by_exchange,
        "buckets": buckets,
        "penny_threshold_usd": 100_000_000,
        "fetched_at": _utc_now_iso(),
    }
    _US_PENNY_CACHE[cache_key] = {"_ts": now_ts, "payload": payload}
    return payload


# Pre-market / after-hours mover scanner — 페니 단타 핵심 (KST 22-23 가장 활성)
_PREMARKET_CACHE: dict = {}
_PREMARKET_TTL_SEC = 60  # 1분 캐시 — yfinance 폴링 부담 완화


@router.get("/premarket/movers")
async def premarket_movers(
    min_change_pct: float = Query(5.0, ge=0, le=200, description="최소 |변동률| %"),
    min_volume: int = Query(1000, ge=0, description="세션 내 누적 거래량 최소"),
    limit: int = Query(50, ge=5, le=100),
    universe: str = Query("penny", description="penny | all"),
    session: str = Query("auto", description="auto / pre / regular / post — auto 는 NY 시각 기반"),
    direction: str = Query("both", description="up | down | both"),
):
    """미국 시장 pre/regular/post 세션 mover 스캐너.

    yfinance batch download → 1분봉 + prepost. 60s 캐시.
    universe="penny" 면 us_stocks where is_penny 만 (~150 종목).
    KST 22-23 = NY pre-market 활성 시각 = 가장 가치 있는 시간대.
    """
    cache_key = f"{universe}|{session}|{min_change_pct}|{min_volume}|{limit}|{direction}"
    now_ts = time.time()
    cached = _PREMARKET_CACHE.get(cache_key)
    if cached and now_ts - cached["_ts"] < _PREMARKET_TTL_SEC:
        return cached["payload"]

    # universe 로딩
    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        if universe == "penny":
            cur = conn.execute(
                """
                SELECT ticker FROM us_stocks
                WHERE is_penny = TRUE
                  AND (is_etf = FALSE OR is_etf IS NULL)
                  AND exchange IN ('NASDAQ','NYSE','NYSE_AMEX')
                  AND market_cap_usd >= 1000000
                ORDER BY market_cap_usd DESC NULLS LAST
                LIMIT 500
                """
            )
        else:
            cur = conn.execute(
                """
                SELECT ticker FROM us_stocks
                WHERE (is_etf = FALSE OR is_etf IS NULL)
                  AND exchange IN ('NASDAQ','NYSE','NYSE_AMEX')
                ORDER BY market_cap_usd DESC NULLS LAST
                LIMIT 1000
                """
            )
        symbols = [r[0] for r in cur.fetchall() if r and r[0]]
    finally:
        conn.close()

    if not symbols:
        payload = {"status": "ok", "session": "empty", "movers": [], "scanned": 0, "matched": 0, "fetched_at": _utc_now_iso()}
        _PREMARKET_CACHE[cache_key] = {"_ts": now_ts, "payload": payload}
        return payload

    from collectors.us_premarket import detect_session
    sess_filter = None if session == "auto" else session
    current_session = sess_filter or detect_session()

    # 1차: us_stock_quote_cache 에서 mover 추출 (DB hit — 외부 API 호출 X)
    # cron worker 가 5분마다 채워서 항상 fresh.
    result_movers = []
    db_hit_count = 0
    yf_fallback_needed = []
    try:
        from server.services.us_quote_cache import ensure_quote_cache_table, QUOTE_CACHE_TABLE
        conn = get_stocks_conn()
        try:
            ensure_quote_cache_table(conn)
            placeholders = ",".join(["?"] * len(symbols))
            cur = conn.execute(
                f"""
                SELECT symbol, current_price, prev_close, change_pct, volume, updated_at
                FROM {QUOTE_CACHE_TABLE}
                WHERE symbol IN ({placeholders}) AND current_price > 0
                """,
                symbols,
            )
            cache_map = {r[0]: r for r in cur.fetchall()}
        finally:
            conn.close()
        for sym in symbols:
            row = cache_map.get(sym)
            if not row:
                yf_fallback_needed.append(sym)
                continue
            cp = float(row[1]) if row[1] is not None else 0
            pc = float(row[2]) if row[2] is not None else 0
            pct = float(row[3]) if row[3] is not None else 0
            vol = int(row[4]) if row[4] is not None else 0
            if abs(pct) < min_change_pct or vol < min_volume:
                continue
            result_movers.append({
                "symbol": sym,
                "last": round(cp, 4),
                "prev_close": round(pc, 4),
                "change_pct": round(pct, 2),
                "volume": vol,
                "session": current_session,
                "source": "db_cache",
            })
        db_hit_count = len(symbols) - len(yf_fallback_needed)
    except Exception as exc:
        # cache 실패 — 전부 yfinance 로
        yf_fallback_needed = list(symbols)

    # 2차: cache miss 종목만 yfinance batch (보통 cron 못 따라간 신규 종목)
    if yf_fallback_needed and (current_session != "closed"):
        from collectors.us_premarket import fetch_premarket_movers
        try:
            yf_result = fetch_premarket_movers(
                yf_fallback_needed,
                min_change_pct=min_change_pct,
                min_volume=min_volume,
                limit=min(limit * 4, 200),
                session_filter=sess_filter,
            )
            for m in yf_result.get("movers") or []:
                m["source"] = "yfinance_fallback"
                result_movers.append(m)
        except Exception as exc:
            # yfinance 도 실패 → cache hit 만으로 반환
            pass

    # 정렬 + direction 필터 + limit
    result_movers.sort(key=lambda m: abs(m.get("change_pct") or 0), reverse=True)
    from datetime import datetime as _dt, timezone as _tz
    from collectors.us_premarket import _to_ny
    ny_now = _to_ny(_dt.now(_tz.utc))
    result = {
        "session": current_session,
        "ny_time": ny_now.strftime("%Y-%m-%d %H:%M:%S ET"),
        "movers": result_movers,
        "scanned": len(symbols),
        "matched": len(result_movers),
        "db_hit": db_hit_count,
        "yf_fallback": len(yf_fallback_needed),
    }

    movers = result.get("movers", [])
    # direction 필터
    if direction == "up":
        movers = [m for m in movers if m.get("change_pct", 0) > 0]
    elif direction == "down":
        movers = [m for m in movers if m.get("change_pct", 0) < 0]
    movers = movers[:limit]

    # 회사명 보강 (universe 가 페니라 작아서 다시 조회)
    name_map: dict = {}
    if movers:
        conn = get_stocks_conn()
        try:
            syms_chunk = [m["symbol"] for m in movers]
            placeholders = ",".join(["?"] * len(syms_chunk))
            cur = conn.execute(
                f"SELECT ticker, name, name_ko, market_cap_usd, is_penny FROM us_stocks WHERE ticker IN ({placeholders})",
                syms_chunk,
            )
            for r in cur.fetchall():
                name_map[r[0]] = {
                    "name": r[1],
                    "name_ko": r[2],
                    "market_cap_usd": float(r[3]) if r[3] is not None else None,
                    "is_penny": bool(r[4]),
                }
        finally:
            conn.close()
    for m in movers:
        meta = name_map.get(m["symbol"]) or {}
        m["name"] = meta.get("name")
        m["name_ko"] = meta.get("name_ko")
        m["market_cap_usd"] = meta.get("market_cap_usd")
        m["is_penny"] = meta.get("is_penny", False)

    payload = {
        "status": "ok",
        "session": result.get("session"),
        "ny_time": result.get("ny_time"),
        "movers": movers,
        "scanned": result.get("scanned", len(symbols)),
        "matched": len(movers),
        "universe": universe,
        "fetched_at": _utc_now_iso(),
    }
    _PREMARKET_CACHE[cache_key] = {"_ts": now_ts, "payload": payload}
    return payload


@router.get("/stocks/{symbol}/pump-dump")
async def stocks_pump_dump(
    symbol: str,
    days: int = Query(365, ge=90, le=730),
    spike_pct: float = Query(30.0, ge=10, le=200),
):
    """과거 펌프&덤프 패턴 — yfinance 일봉 분석 + 후속 결과 통계.

    페니에서 +30%↑ 급등 후 보통 어떻게 됐는지 — D+1/D+7/D+30 분포.
    캐시 30분.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")

    cache_key = f"pump_dump|{sym}|{days}|{spike_pct}"
    now_ts = time.time()
    if not hasattr(stocks_pump_dump, "_cache"):
        stocks_pump_dump._cache = {}
    cached = stocks_pump_dump._cache.get(cache_key)
    if cached and now_ts - cached["_ts"] < 1800:
        return cached["payload"]

    # DB 캐시 일봉 우선 활용 (yfinance rate limit 회피)
    bars_count = 0
    cached_bars = None
    try:
        from server.services.us_price_history_cache import get_db as get_phc_db, ensure_table as _phc_ensure
        from server.db.connections import get_stocks_conn
        conn = get_stocks_conn()
        try:
            _phc_ensure(conn)
            cached_hist = get_phc_db(conn, sym, "1y")
            if cached_hist and cached_hist.get("data"):
                cached_bars = cached_hist["data"]
                bars_count = len(cached_bars)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as exc:
        logger.debug("pump_dump phc cache lookup %s: %s", sym, exc)

    try:
        from collectors.us_pump_dump import fetch_pump_dump_events
        # DB 일봉 충분하면 yfinance 호출 없이 분석 (preloaded_bars)
        res = fetch_pump_dump_events(
            sym,
            days=days,
            spike_threshold_pct=spike_pct,
            preloaded_bars=cached_bars if bars_count >= 30 else None,
        )
    except Exception as exc:
        logger.warning("pump_dump fetch %s failed: %s", sym, exc)
        res = None

    # data_status 판정
    if res and res.get("events"):
        data_status = "ok"
    elif bars_count >= 30:
        # 일봉 충분한데 펌프 이벤트 0건 → 진짜 펌프 이력 없음
        data_status = "no_events"
    elif bars_count > 0:
        # 일봉 일부만 있음 (신규 상장 등)
        data_status = "insufficient_history"
    else:
        # 일봉 자체 미수집
        data_status = "no_history_data"

    if not res:
        res = {"total_events": 0, "events": [], "stats": None}
    res["data_status"] = data_status
    res["bars_available"] = bars_count

    payload = {"status": "ok", "symbol": sym, "data": res, "fetched_at": _utc_now_iso()}
    stocks_pump_dump._cache[cache_key] = {"_ts": now_ts, "payload": payload}
    return payload


@router.get("/stocks/{symbol}/halt-history")
async def stocks_halt_history(symbol: str, days: int = Query(180, ge=7, le=720)):
    """이 종목의 과거 halt 이력 + 익일 종가 변동(있을 때).

    pattern matcher 용. us_halt_events 누적 + yfinance daily 결합.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")
    from server.db.connections import get_stocks_conn
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_stocks_conn()
    try:
        cur = conn.execute(
            """
            SELECT halt_at_utc, expected_resume_at_utc, reason_code, reason_kr, halt_type
            FROM us_halt_events
            WHERE symbol = ? AND halt_at_utc >= ?
            ORDER BY halt_at_utc DESC
            """,
            (sym, cutoff),
        )
        halts = cur.fetchall()
    finally:
        conn.close()

    if not halts:
        # halt 이력 0 — 진짜 무발생 vs 누적 데이터 부족 구분
        conn2 = get_stocks_conn()
        try:
            r = conn2.execute("SELECT MIN(halt_at_utc), COUNT(*) FROM us_halt_events").fetchone()
            earliest = r[0] if r else None
            total_db = int(r[1] or 0) if r else 0
        finally:
            conn2.close()
        from datetime import datetime as _dt, timezone as _tz
        accumulating_days = None
        if earliest and total_db < 100:
            try:
                _e = earliest.replace(tzinfo=_tz.utc) if hasattr(earliest, "tzinfo") and earliest.tzinfo is None else earliest
                accumulating_days = int((_dt.now(_tz.utc) - _e).total_seconds() / 86400)
            except Exception:
                accumulating_days = None
        data_status = "insufficient_accumulation" if (accumulating_days is not None and accumulating_days < 30) else "no_halts"
        return {
            "status": "ok",
            "symbol": sym,
            "data": {
                "halts": [],
                "stats": None,
                "data_status": data_status,
                "accumulating_days": accumulating_days,
                "total_db_events": total_db,
            },
            "fetched_at": _utc_now_iso(),
        }

    # 가격 이력 — 이 함수 안에서 yfinance 호출 (캐시 활용)
    price_by_date: dict[str, float] = {}
    try:
        import yfinance as yf
        hist = yf.Ticker(sym).history(period=f"{days + 30}d", interval="1d", auto_adjust=False)
        if hist is not None and len(hist) > 0:
            for idx, row in hist.iterrows():
                ds = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
                close = float(row.get("Close") or 0)
                if ds and close > 0:
                    price_by_date[ds] = close
    except Exception:
        pass

    sorted_dates = sorted(price_by_date.keys())

    def _next_trading_day(d_str: str) -> str | None:
        for d in sorted_dates:
            if d > d_str:
                return d
        return None

    halt_rows = []
    next_day_returns = []
    week_returns = []
    for h in halts:
        halt_utc = h[0]
        if hasattr(halt_utc, "isoformat"):
            halt_iso = halt_utc.isoformat()
            halt_date = halt_utc.strftime("%Y-%m-%d")
        else:
            halt_iso = str(halt_utc)
            halt_date = halt_iso[:10]

        next_day = _next_trading_day(halt_date)
        same_day_close = price_by_date.get(halt_date)
        next_day_close = price_by_date.get(next_day) if next_day else None
        next_day_ret = None
        if same_day_close and next_day_close:
            next_day_ret = (next_day_close - same_day_close) / same_day_close * 100
            next_day_returns.append(next_day_ret)

        # 5일 후
        d_plus_5 = None
        if next_day:
            try:
                idx = sorted_dates.index(next_day)
                if idx + 4 < len(sorted_dates):
                    d_plus_5 = sorted_dates[idx + 4]
            except ValueError:
                pass
        week_close = price_by_date.get(d_plus_5) if d_plus_5 else None
        week_ret = None
        if same_day_close and week_close:
            week_ret = (week_close - same_day_close) / same_day_close * 100
            week_returns.append(week_ret)

        halt_rows.append({
            "halt_at_utc": halt_iso,
            "halt_date": halt_date,
            "resume_at_utc": h[1].isoformat() if h[1] and hasattr(h[1], "isoformat") else h[1],
            "reason_code": h[2],
            "reason_kr": h[3],
            "halt_type": h[4],
            "close_same_day": round(same_day_close, 4) if same_day_close else None,
            "close_next_day": round(next_day_close, 4) if next_day_close else None,
            "next_day_return_pct": round(next_day_ret, 2) if next_day_ret is not None else None,
            "five_day_return_pct": round(week_ret, 2) if week_ret is not None else None,
        })

    # stats
    def _bucket(returns):
        if not returns:
            return None
        pos = sum(1 for r in returns if r > 0)
        neg = sum(1 for r in returns if r < 0)
        avg = sum(returns) / len(returns)
        big_down = sum(1 for r in returns if r <= -10)
        big_up = sum(1 for r in returns if r >= 10)
        return {
            "count": len(returns),
            "up": pos,
            "down": neg,
            "flat": len(returns) - pos - neg,
            "avg_pct": round(avg, 2),
            "big_down_10pct": big_down,
            "big_up_10pct": big_up,
        }

    stats = {
        "next_day": _bucket(next_day_returns),
        "five_day": _bucket(week_returns),
        "total_halts": len(halts),
    }
    return {"status": "ok", "symbol": sym, "data": {"halts": halt_rows, "stats": stats}, "fetched_at": _utc_now_iso()}


@router.get("/stocks/{symbol}/peers")
async def stocks_peers(symbol: str, limit: int = Query(15, ge=5, le=50), window_pct: int = Query(50, ge=10, le=200)):
    """같은 sector + 시총 ±window_pct% 범위 페니 종목 — 동행 상관 매트릭스.

    이 종목과 동일한 'group' 페니가 오늘 어떻게 움직이는지.
    yfinance fast price 가 us_stocks 에 캐싱돼 있으면 그걸 활용,
    아니면 yfinance 직접 호출 (느림).
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")
    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        cur = conn.execute(
            "SELECT sector, industry, market_cap_usd, name, name_ko, is_penny FROM us_stocks WHERE ticker = ?",
            (sym,),
        )
        r = cur.fetchone()
        if not r:
            return {"status": "ok", "symbol": sym, "data": {"peers": [], "anchor": None}}
        anchor = {
            "sector": r[0],
            "industry": r[1],
            "market_cap_usd": float(r[2]) if r[2] is not None else None,
            "name": r[3],
            "name_ko": r[4],
            "is_penny": bool(r[5]),
        }
        if not anchor["sector"] or not anchor["market_cap_usd"]:
            return {"status": "ok", "symbol": sym, "data": {"peers": [], "anchor": anchor}}
        cap = anchor["market_cap_usd"]
        cap_min = cap * (100 - window_pct) / 100
        cap_max = cap * (100 + window_pct) / 100
        # 같은 industry 우선, 없으면 sector
        cur = conn.execute(
            """
            SELECT ticker, name, name_ko, market_cap_usd, last_price, industry, sector
            FROM us_stocks
            WHERE ticker <> ?
              AND (is_etf = FALSE OR is_etf IS NULL)
              AND exchange IN ('NASDAQ','NYSE','NYSE_AMEX')
              AND sector = ?
              AND market_cap_usd BETWEEN ? AND ?
            ORDER BY
              CASE WHEN industry = ? THEN 0 ELSE 1 END,
              ABS(market_cap_usd - ?) ASC
            LIMIT ?
            """,
            (sym, anchor["sector"], cap_min, cap_max, anchor["industry"] or "", cap, limit),
        )
        peers = [
            {
                "symbol": pr[0],
                "name": pr[1],
                "name_ko": pr[2],
                "market_cap_usd": float(pr[3]) if pr[3] is not None else None,
                "last_price": float(pr[4]) if pr[4] is not None else None,
                "industry": pr[5],
                "sector": pr[6],
            }
            for pr in cur.fetchall()
        ]
    finally:
        conn.close()
    return {"status": "ok", "symbol": sym, "data": {"peers": peers, "anchor": anchor}, "fetched_at": _utc_now_iso()}


def _fetch_snapshot_rows(sub: str, top: int) -> tuple[list[dict], str | None, int | None]:
    """가장 최근 snapshot 의 sub 별 row + snapshot_at + pool_size 반환.

    us_stocks 와 LEFT JOIN 해서 회사명/거래소/섹터 같이 첨부. crypto/us_stocks 에 없는
    ticker 는 name=NULL.

    추가로 comment_bull_n / comment_bear_n / comments_analyzed 도 응답에 포함 (DB 컬럼 신규).
    """
    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        cur = conn.execute(
            "SELECT MAX(snapshot_at) FROM us_reddit_mentions_snapshot WHERE subreddit = ?",
            (sub,),
        )
        row = cur.fetchone()
        latest_at = row[0] if row else None
        if not latest_at:
            return [], None, None

        cur = conn.execute(
            """
            SELECT s.rank, s.symbol, s.mention_count, s.explicit_count, s.score_sum, s.comment_sum,
                   s.avg_upvote_ratio, s.sentiment_score, s.bull_n, s.bear_n, s.kw_matched_posts,
                   s.pool_size, s.by_sub_json, s.top_post_title, s.top_post_url, s.top_post_score,
                   s.comment_bull_n, s.comment_bear_n, s.comments_analyzed,
                   us.name, us.exchange, us.sector, us.name_ko
            FROM us_reddit_mentions_snapshot s
            LEFT JOIN us_stocks us ON UPPER(us.ticker) = UPPER(s.symbol)
            WHERE s.subreddit = ? AND s.snapshot_at = ?
            ORDER BY s.rank ASC
            LIMIT ?
            """,
            (sub, latest_at, int(top)),
        )
        rows = cur.fetchall()
        pool_size = rows[0][11] if rows else None
        import json
        data = []
        for r in rows:
            data.append({
                "rank": r[0],
                "symbol": r[1],
                "mention_count": r[2] or 0,
                "explicit_count": r[3] or 0,
                "score_sum": r[4] or 0,
                "comment_sum": r[5] or 0,
                "avg_upvote_ratio": float(r[6]) if r[6] is not None else 0.5,
                "sentiment_score": r[7] or 50,
                "sentiment_components": {
                    "bull_n": r[8] or 0,
                    "bear_n": r[9] or 0,
                    "kw_matched_posts": r[10] or 0,
                    "comment_bull_n": r[16] or 0,
                    "comment_bear_n": r[17] or 0,
                    "comments_analyzed": r[18] or 0,
                },
                "by_sub": json.loads(r[12]) if r[12] else {},
                "top_post": {
                    "title": r[13], "url": r[14], "score": r[15] or 0,
                } if r[13] else None,
                # us_stocks 매칭 (없으면 None)
                "name": r[19],          # 회사명 (영문)
                "exchange": r[20],      # NASDAQ / NYSE / AMEX
                "sector": r[21],
                "name_ko": r[22],       # 회사명 (한글, 네이버 ac API 기반)
            })
        if hasattr(latest_at, "isoformat"):
            latest_str = latest_at.isoformat()
        else:
            latest_str = str(latest_at)
        return data, latest_str, pool_size
    finally:
        conn.close()


def _fetch_delta_map(sub: str, latest_at_str: str, hours_back: int) -> dict[str, tuple[int, int | None]]:
    """N시간 전 가장 가까운 snapshot 의 symbol → (mention_count, rank) 맵."""
    from server.db.connections import get_stocks_conn
    from datetime import datetime as _dt, timedelta as _td
    try:
        if "T" in latest_at_str:
            latest_dt = _dt.fromisoformat(latest_at_str.split("+")[0].replace("Z", ""))
        else:
            latest_dt = _dt.fromisoformat(latest_at_str)
    except Exception:
        return {}
    target = latest_dt - _td(hours=hours_back)

    conn = get_stocks_conn()
    try:
        window_min = target - _td(minutes=int(60 * hours_back * 0.2) + 30)
        cur = conn.execute(
            """
            SELECT snapshot_at FROM us_reddit_mentions_snapshot
            WHERE subreddit = ? AND snapshot_at <= ? AND snapshot_at >= ?
            ORDER BY snapshot_at DESC
            LIMIT 1
            """,
            (sub, target, window_min),
        )
        r = cur.fetchone()
        if not r:
            return {}
        past_at = r[0]

        cur = conn.execute(
            "SELECT symbol, mention_count, rank FROM us_reddit_mentions_snapshot "
            "WHERE subreddit = ? AND snapshot_at = ?",
            (sub, past_at),
        )
        return {row[0]: (row[1] or 0, row[2]) for row in cur.fetchall()}
    finally:
        conn.close()


def _compute_deltas(cur_m: int, cur_rank: int | None, prev: tuple[int, int | None] | None) -> dict:
    """단일 (현재, 과거) 쌍 → mention/rank delta 딕트."""
    if prev is None:
        return {"abs": None, "pct": None, "rank_change": None, "is_new": False}
    prev_m, prev_rank = prev
    abs_d = cur_m - prev_m
    pct = round(abs_d / prev_m * 100, 1) if prev_m > 0 else None
    # rank_change: 음수면 순위 상승(낮은 숫자), 양수면 하락
    rank_change = (prev_rank - cur_rank) if (prev_rank is not None and cur_rank is not None) else None
    return {"abs": abs_d, "pct": pct, "rank_change": rank_change, "is_new": False}


def _annotate_with_delta(rows: list[dict], sub: str, latest_at: str | None) -> None:
    """rows in-place 에 delta_1h/4h/24h + rank_change_1h/24h + is_new_24h 추가."""
    if not latest_at or not rows:
        return
    map_1h = _fetch_delta_map(sub, latest_at, 1)
    map_4h = _fetch_delta_map(sub, latest_at, 4)
    map_24h = _fetch_delta_map(sub, latest_at, 24)
    has_24h_map = bool(map_24h)
    for r in rows:
        sym = r["symbol"]
        cur_m = r["mention_count"]
        cur_rank = r.get("rank")

        d1 = _compute_deltas(cur_m, cur_rank, map_1h.get(sym))
        d4 = _compute_deltas(cur_m, cur_rank, map_4h.get(sym))
        d24 = _compute_deltas(cur_m, cur_rank, map_24h.get(sym))

        r["delta_1h_abs"] = d1["abs"]
        r["delta_1h_pct"] = d1["pct"]
        r["rank_change_1h"] = d1["rank_change"]

        r["delta_4h_abs"] = d4["abs"]
        r["delta_4h_pct"] = d4["pct"]
        r["rank_change_4h"] = d4["rank_change"]

        r["delta_24h_abs"] = d24["abs"]
        r["delta_24h_pct"] = d24["pct"]
        r["rank_change_24h"] = d24["rank_change"]

        # 24h 전 데이터에 없었으면 신규 진입 (24h map 자체가 비어있으면 판단 불가 → None)
        r["is_new_24h"] = (sym not in map_24h) if has_24h_map else None


@router.get("/reddit/mentions")
async def reddit_mentions_aggregated(
    top: int = Query(25, ge=5, le=50),
):
    """Reddit 통합 ticker mention 랭킹 — DB-backed.

    DB 의 가장 최근 snapshot 에서 통합("__all__") row 반환.
    각 row 에 delta_1h_pct / delta_24h_pct / is_new_24h 자동 첨부.
    """
    cache_key = f"agg|{top}"
    now_ts = time.time()
    cached = _US_REDDIT_MENTIONS_CACHE.get(cache_key)
    if cached and now_ts - cached["_ts"] < _US_REDDIT_MENTIONS_TTL_SEC:
        return cached["payload"]

    rows, latest_at, pool_size = _fetch_snapshot_rows("__all__", int(top))
    if not rows:
        # snapshot 없으면 즉시 fetch (cron 첫 실행 전 fallback)
        try:
            from collectors.us_reddit_mentions import get_mentions_aggregated
            res = get_mentions_aggregated(top_n=int(top), min_mentions=2)
            rows = res.get("data", [])
            pool_size = res.get("post_pool_size", 0)
            latest_at = res.get("as_of")
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"no snapshot and live fetch failed: {exc}")

    _annotate_with_delta(rows, "__all__", latest_at)

    payload = {
        "status": "ok",
        "subreddits": ["wallstreetbets", "stocks", "options", "investing", "StockMarket"],
        "data": rows,
        "post_pool_size": pool_size or 0,
        "snapshot_at": latest_at,
        "fetched_at": _utc_now_iso(),
        "source": "db_snapshot",
    }
    _US_REDDIT_MENTIONS_CACHE[cache_key] = {"_ts": now_ts, "payload": payload}
    return payload


@router.get("/reddit/mentions/{sub}")
async def reddit_mentions_by_sub(
    sub: str,
    top: int = Query(25, ge=5, le=50),
):
    """단일 서브의 ticker mention 랭킹 — DB snapshot 기반 + delta."""
    sub_name = (sub or "").strip()
    if not sub_name:
        raise HTTPException(status_code=400, detail="sub required")

    cache_key = f"sub|{sub_name}|{top}"
    now_ts = time.time()
    cached = _US_REDDIT_MENTIONS_CACHE.get(cache_key)
    if cached and now_ts - cached["_ts"] < _US_REDDIT_MENTIONS_TTL_SEC:
        return cached["payload"]

    rows, latest_at, pool_size = _fetch_snapshot_rows(sub_name, int(top))
    if not rows:
        try:
            from collectors.us_reddit_mentions import get_mentions_by_sub
            res = get_mentions_by_sub(sub=sub_name, top_n=int(top), min_mentions=1)
            rows = res.get("data", [])
            pool_size = res.get("post_pool_size", 0)
            latest_at = res.get("as_of")
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"no snapshot and live fetch failed: {exc}")

    _annotate_with_delta(rows, sub_name, latest_at)

    payload = {
        "status": "ok",
        "subreddit": sub_name,
        "data": rows,
        "post_pool_size": pool_size or 0,
        "snapshot_at": latest_at,
        "fetched_at": _utc_now_iso(),
        "source": "db_snapshot",
    }
    _US_REDDIT_MENTIONS_CACHE[cache_key] = {"_ts": now_ts, "payload": payload}
    return payload


@router.get("/reddit/symbol-history/{symbol}")
async def reddit_symbol_history(
    symbol: str,
    sub: str = Query("__all__"),
    hours: int = Query(48, ge=6, le=720),
):
    """단일 종목의 mention 시계열 — 24h / 72h 차트용."""
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")
    from server.db.connections import get_stocks_conn
    from datetime import datetime as _dt, timedelta as _td
    cutoff = _dt.utcnow() - _td(hours=int(hours))

    conn = get_stocks_conn()
    try:
        cur = conn.execute(
            """
            SELECT snapshot_at, mention_count, sentiment_score, rank
            FROM us_reddit_mentions_snapshot
            WHERE symbol = ? AND subreddit = ? AND snapshot_at >= ?
            ORDER BY snapshot_at ASC
            """,
            (sym, sub, cutoff),
        )
        rows = cur.fetchall()
        data = [
            {
                "snapshot_at": r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0]),
                "mention_count": r[1] or 0,
                "sentiment_score": r[2] or 50,
                "rank": r[3],
            }
            for r in rows
        ]
    finally:
        conn.close()

    return {
        "status": "ok",
        "symbol": sym,
        "subreddit": sub,
        "hours": int(hours),
        "count": len(data),
        "data": data,
        "fetched_at": _utc_now_iso(),
    }


@router.get("/reddit/risers")
async def reddit_risers(
    sub: str = Query("__all__"),
    top: int = Query(10, ge=3, le=30),
    min_mentions: int = Query(3, ge=1, le=20),
):
    """1시간 mention 증가율 TOP — 급상승 종목.

    apewisdom 의 "rising stocks" 와 동일. squeeze 후보 발견.
    """
    rows, latest_at, _ = _fetch_snapshot_rows(sub, 100)
    if not rows or not latest_at:
        return {"status": "ok", "data": [], "snapshot_at": None, "fetched_at": _utc_now_iso()}

    map_1h = _fetch_delta_map(sub, latest_at, 1)

    candidates = []
    for r in rows:
        if r["mention_count"] < min_mentions:
            continue
        prev = map_1h.get(r["symbol"])
        if prev is None:
            r["delta_1h_abs"] = r["mention_count"]
            r["delta_1h_pct"] = None
            r["is_new_1h"] = True
        else:
            prev_m, _ = prev
            delta = r["mention_count"] - prev_m
            if delta <= 0:
                continue
            r["delta_1h_abs"] = delta
            r["delta_1h_pct"] = round(delta / prev_m * 100, 1) if prev_m else None
            r["is_new_1h"] = False
        candidates.append(r)

    # 정렬 — 신규는 mention_count, 기존은 delta_pct (None 처리)
    candidates.sort(
        key=lambda r: (
            r.get("is_new_1h", False),
            r.get("delta_1h_pct") or 0,
            r.get("delta_1h_abs") or 0,
        ),
        reverse=True,
    )

    return {
        "status": "ok",
        "subreddit": sub,
        "data": candidates[: int(top)],
        "snapshot_at": latest_at,
        "fetched_at": _utc_now_iso(),
    }


@router.get("/reddit/search")
async def reddit_search(
    q: str = Query(..., min_length=1, max_length=120, description="검색어 (ticker, 키워드, 자유)"),
    subs: str = Query("wallstreetbets,stocks,options,investing,StockMarket", description="콤마 구분 sub list"),
    sort: str = Query("relevance", description="relevance / new / top / hot"),
    time_range: str = Query("week", description="hour / day / week / month / year / all"),
    limit: int = Query(30, ge=1, le=100, description="총 결과 개수"),
):
    """Reddit 실시간 검색 — 다중 sub 병렬 fetch + 통합 정렬.

    임의 ticker/키워드/문장 검색 가능. 풀(hot/new/rising) 의존 X.
    캐시 90초 (Reddit rate limit 보호).
    """
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query required")
    sub_list = [s.strip() for s in subs.split(",") if s.strip()]
    if not sub_list:
        sub_list = ["wallstreetbets"]

    cache_key = f"{query.lower()}|{','.join(sorted(sub_list))}|{sort}|{time_range}|{limit}"
    now_ts = time.time()
    cached = _US_REDDIT_SEARCH_CACHE.get(cache_key)
    if cached and now_ts - cached["_ts"] < _US_REDDIT_SEARCH_TTL_SEC:
        return cached["payload"]

    try:
        from collectors.us_reddit_search import search_reddit
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"reddit_search import failed: {exc}")
    try:
        res = search_reddit(
            query=query, subs=sub_list, sort=sort,
            time_range=time_range, total_limit=int(limit),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    payload = {
        "status": "ok",
        **res,
        "fetched_at": _utc_now_iso(),
        "source": "reddit_search_api",
    }
    _US_REDDIT_SEARCH_CACHE[cache_key] = {"_ts": now_ts, "payload": payload}
    return payload


@router.get("/reddit/symbol/{symbol}")
async def reddit_search_for_symbol(
    symbol: str,
    subs: str = Query("wallstreetbets,stocks,options,investing,StockMarket"),
    time_range: str = Query("week"),
    limit: int = Query(20, ge=1, le=50),
):
    """단일 종목 실시간 Reddit 검색 — post-filter 로 ticker 명확 매칭만 통과.

    USWsbCard (종목 상세) 가 이걸로 hot/new/rising 풀 의존 제거.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")
    sub_list = [s.strip() for s in subs.split(",") if s.strip()]

    cache_key = f"sym|{sym}|{','.join(sorted(sub_list))}|{time_range}|{limit}"
    now_ts = time.time()
    cached = _US_REDDIT_SEARCH_CACHE.get(cache_key)
    if cached and now_ts - cached["_ts"] < _US_REDDIT_SEARCH_TTL_SEC:
        return cached["payload"]

    try:
        from collectors.us_reddit_search import search_for_symbol
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"reddit_search import failed: {exc}")
    try:
        res = search_for_symbol(symbol=sym, subs=sub_list, time_range=time_range, limit=int(limit))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    payload = {
        "status": "ok",
        **res,
        "fetched_at": _utc_now_iso(),
        "source": "reddit_search_api",
    }
    _US_REDDIT_SEARCH_CACHE[cache_key] = {"_ts": now_ts, "payload": payload}
    return payload


@router.get("/wsb/top")
async def get_wsb_top(
    limit: int = Query(15, ge=1, le=30),
    min_mentions: int = Query(2, ge=1, le=10),
):
    """WallStreetBets (r/wallstreetbets) 핫 ticker TOP N.

    hot/new/rising 통합 ~150~250 게시물에서 $TICKER 명시 + ALL-CAPS(us_stocks 검증) 추출.
    홈 위젯 "WSB 핫 종목" 에서 호출.
    """
    key = f"top_{limit}_{min_mentions}"
    now_ts = time.time()
    cached = _US_WSB_TOP_CACHE.get(key)
    if cached and now_ts - cached["_ts"] < _US_WSB_TTL_SEC:
        return cached["payload"]

    try:
        from collectors.us_wsb_sentiment import get_wsb_top_mentions
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"wsb import failed: {exc}")
    try:
        res = get_wsb_top_mentions(top_n=int(limit), min_mentions=int(min_mentions))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    payload = {
        "status": "ok",
        "count": len(res.get("data", [])),
        "data": res.get("data", []),
        "post_pool_size": res.get("post_pool_size", 0),
        "as_of": res.get("as_of"),
        "fetched_at": _utc_now_iso(),
        "source": "reddit_r_wallstreetbets",
    }
    _US_WSB_TOP_CACHE[key] = {"_ts": now_ts, "payload": payload}
    return payload


@router.get("/wsb/{symbol}")
async def get_wsb_for_symbol(symbol: str, limit: int = Query(10, ge=1, le=30)):
    """단일 종목의 WSB 언급 (최근 hot/new/rising 풀 내).

    종목 상세 페이지 SOCIAL 섹션에서 호출.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")

    key = f"{sym}_{limit}"
    now_ts = time.time()
    cached = _US_WSB_SYM_CACHE.get(key)
    if cached and now_ts - cached["_ts"] < _US_WSB_TTL_SEC:
        return cached["payload"]

    try:
        from collectors.us_wsb_sentiment import get_wsb_for_symbol as _get_wsb_for_symbol
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"wsb import failed: {exc}")
    try:
        res = _get_wsb_for_symbol(sym, limit=int(limit))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    payload = {
        "status": "ok",
        **res,
        "fetched_at": _utc_now_iso(),
        "source": "reddit_r_wallstreetbets",
    }
    _US_WSB_SYM_CACHE[key] = {"_ts": now_ts, "payload": payload}
    return payload


@router.get("/orderbook/{symbol}")
async def get_orderbook(symbol: str):
    """미국 주식 호가창 (Top of Book) — bid/ask + bidSize/askSize + 매수/매도 우위.

    KIS overseas 의 다중 호가 endpoint 가 권한 미지원 → yfinance NBBO Top of Book.
    Level 2 (depth book) 는 미제공. spread + imbalance 로 매수/매도 압력만 표현.

    응답에는 매수/매도 우위 라벨(매수 강세 / 매수 우위 / 균형 / 매도 우위 / 매도 강세) 포함.
    프론트의 USOrderBookCard 가 이 endpoint 를 호출.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")

    # 3분 캐시 — yfinance Ticker.info 호출은 1~5초 무거움. 지연 데이터라 3분 stale 무해.
    now_ts = time.time()
    cached = _US_ORDERBOOK_CACHE.get(sym)
    if cached and now_ts - cached["_ts"] < _US_ORDERBOOK_TTL_SEC:
        return cached["payload"]

    try:
        from collectors.us_orderbook import get_orderbook as _get_orderbook
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"orderbook import failed: {exc}")
    try:
        data = _get_orderbook(sym)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"orderbook fetch failed: {exc}")

    payload = {
        "status": "ok",
        "data": data,
        "fetched_at": _utc_now_iso(),
        "cache_ttl_sec": _US_ORDERBOOK_TTL_SEC,
    }
    _US_ORDERBOOK_CACHE[sym] = {"_ts": now_ts, "payload": payload}
    return payload


@router.get("/ftd/{symbol}")
async def get_ftd_history(
    symbol: str,
    days: int = Query(60, ge=7, le=365),
):
    """SEC Reg SHO Fail-to-Deliver — 종목 결제실패 일별 시계열.

    Threshold Securities 등재 전 단계 시그널. FTD 증가 = squeeze 압력 누적.
    SEC 데이터는 T+1 약 2주 지연.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")
    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    days_int = max(7, min(365, int(days)))
    try:
        cur = conn.execute(
            f"""
            SELECT settlement_date, fail_quantity, price, description
            FROM us_ftd_daily
            WHERE symbol = ?
              AND settlement_date >= (CURRENT_DATE - INTERVAL '{days_int} days')::text
            ORDER BY settlement_date DESC
            LIMIT 365
            """,
            (sym,),
        )
        rows = cur.fetchall()
        data = [
            {
                "settlement_date": r[0],
                "fail_quantity": int(r[1]) if r[1] is not None else None,
                "price": float(r[2]) if r[2] is not None else None,
                "description": r[3],
            }
            for r in rows
        ]
    finally:
        conn.close()

    # 요약 통계
    qty_total = sum(d["fail_quantity"] or 0 for d in data)
    qty_avg = round(qty_total / len(data)) if data else 0
    qty_max = max((d["fail_quantity"] or 0 for d in data), default=0)
    last_date = data[0]["settlement_date"] if data else None

    return {
        "status": "ok",
        "symbol": sym,
        "count": len(data),
        "data": data,
        "summary": {
            "total_fails": qty_total,
            "avg_daily_fails": qty_avg,
            "max_daily_fails": qty_max,
            "last_settlement_date": last_date,
        },
        "fetched_at": _utc_now_iso(),
        "source": "sec_edgar_cnsfails",
    }


@router.get("/ftd-top")
async def get_ftd_top(
    days: int = Query(30, ge=7, le=180),
    limit: int = Query(20, ge=1, le=100),
):
    """최근 N일 종목별 FTD 합계 TOP — squeeze 압력 누적 랭킹.

    threshold 등재 진입을 미리 예측하는 leading indicator.
    """
    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    days_int = max(7, min(180, int(days)))
    limit_int = max(1, min(100, int(limit)))
    try:
        cur = conn.execute(
            f"""
            SELECT symbol,
                   SUM(fail_quantity) as total_fails,
                   MAX(fail_quantity) as max_daily_fails,
                   COUNT(*) as days_count,
                   AVG(price) as avg_price,
                   MAX(settlement_date) as last_date
            FROM us_ftd_daily
            WHERE settlement_date >= (CURRENT_DATE - INTERVAL '{days_int} days')::text
              AND fail_quantity > 0
            GROUP BY symbol
            HAVING SUM(fail_quantity) > 10000
            ORDER BY total_fails DESC
            LIMIT {limit_int}
            """,
        )
        rows = cur.fetchall()
        data = [
            {
                "symbol": r[0],
                "total_fails": int(r[1]),
                "max_daily_fails": int(r[2]),
                "days_count": int(r[3]),
                "avg_price": float(r[4]) if r[4] is not None else None,
                "last_settlement_date": r[5],
            }
            for r in rows
        ]
    finally:
        conn.close()
    return {
        "status": "ok",
        "lookback_days": int(days),
        "count": len(data),
        "data": data,
        "fetched_at": _utc_now_iso(),
        "source": "sec_edgar_cnsfails",
    }


@router.get("/short-volume/{symbol}")
async def get_short_volume_history(
    symbol: str,
    days: int = Query(14, ge=1, le=60),
):
    """FINRA Reg SHO Daily Short Sale Volume — 종목 단위 최근 N일치.

    당일 거래 중 공매도 비중 (short_volume_ratio) 을 일별로 반환. 트레이더가
    "오늘 매도 폭주의 몇 %가 공매도였나" 판단할 때 핵심 시그널.
    """
    from server.db.connections import get_stocks_conn
    sym = symbol.strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")
    conn = get_stocks_conn()
    try:
        cur = conn.execute(
            """
            SELECT trade_date, short_volume, short_exempt_volume,
                   total_volume, short_volume_ratio, market
            FROM us_short_volume_daily
            WHERE symbol = %s
            ORDER BY trade_date DESC
            LIMIT %s
            """,
            (sym, days),
        )
        rows = cur.fetchall()
        cols = ["trade_date", "short_volume", "short_exempt_volume",
                "total_volume", "short_volume_ratio", "market"]
        data = [
            dict(zip(cols, r))
            for r in rows
        ]
    finally:
        conn.close()
    return {
        "status": "ok",
        "symbol": sym,
        "data": data,
        "fetched_at": _utc_now_iso(),
    }


@router.get("/short-history/{symbol}")
async def get_short_history(symbol: str, days: int = Query(90, ge=7, le=365)):
    """공매도 잔고(SI%)·DTC·차입 이자율(borrow fee) 일별 시계열.

    us_short_interest_daily + us_short_borrow_daily 누적 데이터를 합쳐 반환.
    FINTEL 의 short interest history 차트에 해당 — 공매도 압력 추이 시각화.
    오래된→최신 순으로 정렬해 반환 (차트 친화).
    """
    from server.db.connections import get_stocks_conn
    sym = symbol.strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")
    conn = get_stocks_conn()
    try:
        cur = conn.execute(
            """
            SELECT as_of_date, short_float_pct, days_to_cover, short_interest_shares
            FROM us_short_interest_daily
            WHERE symbol = %s
            ORDER BY as_of_date DESC
            LIMIT %s
            """,
            (sym, days),
        )
        si_cols = ["as_of_date", "short_float_pct", "days_to_cover", "short_interest_shares"]
        short_interest = [dict(zip(si_cols, r)) for r in cur.fetchall()]

        cur = conn.execute(
            """
            SELECT as_of_date, borrow_fee_pct, available_shares, rebate_rate_pct
            FROM us_short_borrow_daily
            WHERE symbol = %s
            ORDER BY as_of_date DESC
            LIMIT %s
            """,
            (sym, days),
        )
        bo_cols = ["as_of_date", "borrow_fee_pct", "available_shares", "rebate_rate_pct"]
        borrow = [dict(zip(bo_cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()

    short_interest.reverse()
    borrow.reverse()
    return {
        "status": "ok",
        "symbol": sym,
        "short_interest": short_interest,
        "borrow": borrow,
        "fetched_at": _utc_now_iso(),
    }


@router.get("/flow-proxy/{symbol}")
async def get_flow_proxy(symbol: str):
    """매수/매도 압력 프록시 — 분봉 상승/하락 봉 거래량 비율.

    진짜 체결 매수/매도 구분(틱 데이터)은 무료 소스로 불가. 대안으로 1분봉의
    종가>시가(매수 우위 봉) vs 종가<시가(매도 우위 봉) 거래량을 합산해 추정.
    us_minute_chart_cache 1분봉 사용 — 페이지 진입 시 외부 호출 없음.
    """
    from server.db.connections import get_stocks_conn
    from server.services.us_minute_chart_cache import get_minute_chart_cached
    sym = symbol.strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")
    conn = get_stocks_conn()
    try:
        chart = get_minute_chart_cached(conn, sym, nmin=1, nrec=390)
    finally:
        conn.close()
    bars = (chart or {}).get("data") or []

    def _classify(bar_list):
        up_v = down_v = flat_v = 0.0
        for b in bar_list:
            try:
                o = float(b.get("open"))
                c = float(b.get("close"))
                v = float(b.get("volume") or 0)
            except (TypeError, ValueError):
                continue
            if v <= 0:
                continue
            if c > o:
                up_v += v
            elif c < o:
                down_v += v
            else:
                flat_v += v
        tot = up_v + down_v
        return {
            "up_volume": int(up_v),
            "down_volume": int(down_v),
            "flat_volume": int(flat_v),
            "buy_pressure": round(up_v / tot, 4) if tot > 0 else None,
            "bar_count": len(bar_list),
        }

    session = None
    session_date = None
    if bars:
        session_date = bars[-1].get("date")
        session_bars = [b for b in bars if b.get("date") == session_date]
        session = _classify(session_bars)

    return {
        "status": "ok",
        "symbol": sym,
        "session_date": session_date,
        "session": session,
        "recent": _classify(bars) if bars else None,
        "fetched_at": _utc_now_iso(),
    }


@router.get("/squeeze/top")
async def get_squeeze_top(
    limit: int = Query(20, ge=1, le=100),
    min_volume: float = Query(500_000, ge=0),
    require_si: bool = Query(True, description="SI% 데이터 있는 종목만"),
    require_borrow: bool = Query(False, description="iBorrowDesk 차입 데이터 있는 종목만"),
):
    """미국 종목 Squeeze Score TOP N — 4테이블 JOIN.

    Score 공식 (0~200):
      short_volume_ratio × 40   : 어제 공매도 비중 (실시간 시그널)
      SI% × 1.5 (cap 30)        : 누적 SI 부담
      DTC × 3 (cap 10)          : 청산 어려움
      CTB × 0.5 (cap 50)        : 차입 비용 부담
    """
    try:
        from scripts.compute_us_squeeze_score import compute_scores  # type: ignore
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"compute import failed: {exc}")
    try:
        rows = compute_scores(min_total_volume=float(min_volume))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if require_si:
        rows = [r for r in rows if r.get("short_float_pct") is not None]
    if require_borrow:
        rows = [r for r in rows if r.get("borrow_fee_pct") is not None]
    rows.sort(key=lambda r: r["squeeze_score"], reverse=True)
    return {
        "status": "ok",
        "count": len(rows[:limit]),
        "total_universe": len(rows),
        "data": rows[:limit],
        "as_of": rows[0]["trade_date"] if rows else None,
        "fetched_at": _utc_now_iso(),
    }


@router.get("/halts/recent")
async def get_halts_recent(
    hours: int = Query(24, ge=1, le=72),
    limit: int = Query(50, ge=1, le=200),
    active_only: bool = Query(False, description="아직 재개 안 된 halt 만"),
):
    """미국 거래정지(LULD pause·SEC halt 등) — NASDAQ Trader RSS.

    한국 시장의 사이드카(상킷/하킷)에 해당 = LUDP/LUDS/T5 (5분 자동 정지).
    프론트에서 USStockDetailView 의 sticky 배너 / 홈 위젯이 호출.
    """
    try:
        from collectors.us_trade_halts import get_recent_halts
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"halts import failed: {exc}")
    try:
        halts = get_recent_halts(active_only=active_only, hours=int(hours), max_items=int(limit))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {
        "status": "ok",
        "count": len(halts),
        "data": halts,
        "fetched_at": _utc_now_iso(),
    }


@router.get("/halts/{symbol}")
async def get_halts_for_symbol(symbol: str, hours: int = Query(48, ge=1, le=168)):
    """단일 종목의 최근 halt 이력. 종목 상세 페이지의 timeline 용."""
    try:
        from collectors.us_trade_halts import get_recent_halts
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"halts import failed: {exc}")
    sym = symbol.strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")
    halts = get_recent_halts(active_only=False, hours=int(hours), max_items=200)
    matched = [h for h in halts if h.get("symbol", "").upper() == sym]
    return {
        "status": "ok",
        "symbol": sym,
        "count": len(matched),
        "data": matched,
        "fetched_at": _utc_now_iso(),
    }


@router.get("/threshold/latest")
async def get_threshold_latest(
    limit: int = Query(20, ge=1, le=100),
    days: int = Query(7, ge=1, le=30, description="며칠 backlog까지 포함"),
):
    """NYSE Reg SHO Threshold 등재 종목 — 최근 N일 누적 (중복 제거).

    홈의 "오늘의 Threshold 종목" 카드가 호출. 종목별 가장 최근 등재일·시장 표시.
    """
    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        cur = conn.execute(
            """
            SELECT t.symbol, t.name, t.market, t.market_category, t.as_of_date,
                   sv.short_volume_ratio, sv.total_volume,
                   si.short_float_pct, si.days_to_cover
            FROM (
                SELECT DISTINCT ON (symbol)
                       symbol, name, market, market_category, as_of_date
                FROM us_threshold_securities_daily
                WHERE as_of_date >= (CURRENT_DATE - INTERVAL '%s days')::text
                ORDER BY symbol, as_of_date DESC
            ) t
            LEFT JOIN (
                SELECT DISTINCT ON (symbol) symbol, short_volume_ratio, total_volume
                FROM us_short_volume_daily
                ORDER BY symbol, trade_date DESC
            ) sv ON sv.symbol = t.symbol
            LEFT JOIN (
                SELECT DISTINCT ON (symbol) symbol, short_float_pct, days_to_cover
                FROM us_short_interest_daily
                ORDER BY symbol, as_of_date DESC
            ) si ON si.symbol = t.symbol
            ORDER BY t.as_of_date DESC, t.symbol ASC
            LIMIT %s
            """,
            (int(days), int(limit)),
        )
        rows = cur.fetchall()
        cols = ["symbol", "name", "market", "market_category", "as_of_date",
                "short_volume_ratio", "total_volume",
                "short_float_pct", "days_to_cover"]
        data = [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()
    return {
        "status": "ok",
        "count": len(data),
        "data": data,
        "fetched_at": _utc_now_iso(),
    }


@router.get("/squeeze/{symbol}")
async def get_squeeze_detail(symbol: str):
    """단일 종목 squeeze score + 구성 요소 (4테이블 데이터 통합).

    USStockDetailView 의 Squeeze 위젯이 호출.
    """
    from server.db.connections import get_stocks_conn
    sym = symbol.strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")
    conn = get_stocks_conn()
    try:
        # short_volume (가장 최근 trade_date)
        cur = conn.execute(
            """
            SELECT trade_date, short_volume, short_exempt_volume, total_volume, short_volume_ratio
            FROM us_short_volume_daily
            WHERE symbol = %s
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            (sym,),
        )
        sv_row = cur.fetchone()
        sv = None
        if sv_row:
            sv = {
                "trade_date": sv_row[0],
                "short_volume": sv_row[1],
                "short_exempt_volume": sv_row[2],
                "total_volume": sv_row[3],
                "short_volume_ratio": sv_row[4],
            }
        # short interest (Finviz)
        cur = conn.execute(
            """
            SELECT as_of_date, short_float_pct, short_interest_shares, days_to_cover
            FROM us_short_interest_daily
            WHERE symbol = %s
            ORDER BY as_of_date DESC
            LIMIT 1
            """,
            (sym,),
        )
        si_row = cur.fetchone()
        si = None
        if si_row:
            si = {
                "as_of_date": si_row[0],
                "short_float_pct": si_row[1],
                "short_interest_shares": si_row[2],
                "days_to_cover": si_row[3],
            }
        # borrow (iBorrowDesk)
        cur = conn.execute(
            """
            SELECT as_of_date, available_shares, borrow_fee_pct, rebate_rate_pct
            FROM us_short_borrow_daily
            WHERE symbol = %s
            ORDER BY as_of_date DESC
            LIMIT 1
            """,
            (sym,),
        )
        bw_row = cur.fetchone()
        bw = None
        if bw_row:
            bw = {
                "as_of_date": bw_row[0],
                "available_shares": bw_row[1],
                "borrow_fee_pct": bw_row[2],
                "rebate_rate_pct": bw_row[3],
            }
        # ownership
        cur = conn.execute(
            """
            SELECT as_of_date, institutional_ownership_pct, insider_ownership_pct
            FROM us_ownership_daily
            WHERE symbol = %s
            ORDER BY as_of_date DESC
            LIMIT 1
            """,
            (sym,),
        )
        ow_row = cur.fetchone()
        ow = None
        if ow_row:
            ow = {
                "as_of_date": ow_row[0],
                "institutional_ownership_pct": ow_row[1],
                "insider_ownership_pct": ow_row[2],
            }
        # NYSE Threshold Securities — 가장 최근 진입일자 (없으면 None)
        cur = conn.execute(
            """
            SELECT as_of_date, market, market_category, name
            FROM us_threshold_securities_daily
            WHERE symbol = %s
            ORDER BY as_of_date DESC
            LIMIT 1
            """,
            (sym,),
        )
        th_row = cur.fetchone()
        threshold = None
        if th_row:
            threshold = {
                "last_date": th_row[0],
                "market": th_row[1],
                "market_category": th_row[2],
                "name": th_row[3],
            }
    finally:
        conn.close()

    # Squeeze Score 계산 (overseas/squeeze/top 와 동일 공식)
    score = 0.0
    if sv and sv["short_volume_ratio"] is not None:
        score += min(float(sv["short_volume_ratio"]), 1.0) * 40
    if si and si["short_float_pct"] is not None:
        score += min(float(si["short_float_pct"]), 30) * 1.5
    if si and si["days_to_cover"] is not None:
        score += min(float(si["days_to_cover"]), 10) * 3
    if bw and bw["borrow_fee_pct"] is not None:
        score += min(float(bw["borrow_fee_pct"]), 50) * 0.5
    if threshold:
        score += 10  # threshold bonus — 강제 buy-in 압력 누적

    return {
        "status": "ok",
        "symbol": sym,
        "squeeze_score": round(score, 1),
        "short_volume": sv,
        "short_interest": si,
        "borrow": bw,
        "ownership": ow,
        "threshold": threshold,
        "fetched_at": _utc_now_iso(),
    }


@router.get("/status")
async def get_status() -> Dict[str, Any]:
    return {
        "status": "ok",
        "market_status": get_us_market_status(),
        "ws": kis_ws_proxy.status(),
    }


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if await reject_websocket_if_unauthorized(websocket):
        return
    await websocket.accept()
    await kis_ws_proxy.add_client(websocket)
    await websocket.send_text(
        json.dumps(
            {
                "type": "welcome",
                "message": "Send subscribe/unsubscribe messages: {'action':'subscribe','symbol':'AAPL','channels':['trade','orderbook']}",
                "market_status": get_us_market_status(),
            }
        )
    )

    try:
        while True:
            message = await websocket.receive_text()
            await kis_ws_proxy.handle_client_message(websocket, message)
    except WebSocketDisconnect:
        await kis_ws_proxy.remove_client(websocket)
    except Exception as exc:
        logger.error("Overseas websocket error: %s", exc)
        await kis_ws_proxy.remove_client(websocket)
