"""
미국 테마 구성 종목 시세 일괄 수집기 (yfinance)

stock_themes_us에 등록된 모든 티커에 대해 yfinance로
- us_stocks : name/exchange/sector/industry/market_cap
- price_today_us : current_price/change_pct/trading_volume/trading_value 등
을 갱신한다.

- Postgres 연결 (server.db.connections.get_stocks_conn 사용)

실행:
  python collectors/us_theme_price_collector.py           # 전체 (시세 + 메타)
  python collectors/us_theme_price_collector.py --quick   # 시세만
"""
from __future__ import annotations

import os
import sys
import time
import math
from datetime import datetime
from typing import Any, Dict, List, Optional

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)


def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        if isinstance(v, float) and math.isnan(v):
            return None
        text = str(v).strip()
        if text in ("", "-", "None", "nan", "NaN"):
            return None
        return float(text.replace(",", ""))
    except Exception:
        return None


def _get_conn():
    from server.db.connections import get_stocks_conn
    return get_stocks_conn()


def _all_tickers(conn) -> List[str]:
    rows = conn.execute("SELECT DISTINCT ticker FROM stock_themes_us ORDER BY ticker").fetchall()
    return [r[0] for r in rows]


def _batch_download(tickers: List[str], period: str = "5d") -> Dict[str, Dict[str, Optional[float]]]:
    import yfinance as yf

    if not tickers:
        return {}

    df = yf.download(
        tickers=" ".join(tickers),
        period=period,
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        progress=False,
        threads=True,
    )

    result: Dict[str, Dict[str, Optional[float]]] = {}
    if df is None or len(df) == 0:
        return result

    single = len(tickers) == 1
    for t in tickers:
        try:
            if single:
                sub = df
            else:
                if t not in df.columns.get_level_values(0):
                    continue
                sub = df[t]
            sub = sub.dropna(how="all")
            if len(sub) == 0:
                continue
            last = sub.iloc[-1]
            prev = sub.iloc[-2] if len(sub) >= 2 else None
            close = _to_float(last.get("Close"))
            open_ = _to_float(last.get("Open"))
            high = _to_float(last.get("High"))
            low = _to_float(last.get("Low"))
            volume = _to_float(last.get("Volume"))
            prev_close = _to_float(prev.get("Close")) if prev is not None else None
            result[t] = {
                "close": close,
                "open": open_,
                "high": high,
                "low": low,
                "volume": volume,
                "prev_close": prev_close,
            }
        except Exception:
            continue
    return result


def _fetch_meta(ticker: str) -> Dict[str, Any]:
    import yfinance as yf

    try:
        tk = yf.Ticker(ticker)
        info = tk.info or {}
    except Exception:
        info = {}
    return {
        "name": info.get("shortName") or info.get("longName") or ticker,
        "exchange": info.get("exchange") or info.get("fullExchangeName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "market_cap": _to_float(info.get("marketCap")),
    }


_UPSERT_PRICE_SQL = """
INSERT INTO price_today_us (
    ticker, current_price, change_pct, change_amt, prev_close,
    open_price, day_high, day_low,
    trading_volume, trading_value, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(ticker) DO UPDATE SET
    current_price=excluded.current_price,
    change_pct=excluded.change_pct,
    change_amt=excluded.change_amt,
    prev_close=excluded.prev_close,
    open_price=excluded.open_price,
    day_high=excluded.day_high,
    day_low=excluded.day_low,
    trading_volume=excluded.trading_volume,
    trading_value=excluded.trading_value,
    updated_at=excluded.updated_at
"""

_UPSERT_META_SQL = """
INSERT INTO us_stocks (ticker, name, exchange, sector, industry, market_cap, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(ticker) DO UPDATE SET
    name=excluded.name,
    exchange=excluded.exchange,
    sector=excluded.sector,
    industry=excluded.industry,
    market_cap=excluded.market_cap,
    updated_at=excluded.updated_at
"""


def _safe_commit(conn) -> None:
    try:
        conn.commit()
    except Exception:
        pass


def run(fill_meta: bool = True, meta_sleep: float = 0.05) -> Dict[str, Any]:
    conn = _get_conn()
    tickers = _all_tickers(conn)
    if not tickers:
        print("[us-theme] stock_themes_us 비어있음. scripts/parse_themes_us.py 먼저 실행.")
        return {"tickers": 0, "priced": 0, "meta": 0}

    print(f"[us-theme] 대상 티커: {len(tickers)}개")
    print(f"[us-theme] 시세 배치 다운로드 시작...")
    t0 = time.time()
    priced = 0
    now = datetime.utcnow().isoformat() + "Z"
    CHUNK = 150

    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i : i + CHUNK]
        prices = _batch_download(chunk, period="5d")
        for t in chunk:
            p = prices.get(t)
            if not p:
                continue
            close = p.get("close")
            prev_close = p.get("prev_close")
            change_amt = None
            change_pct = None
            if close is not None and prev_close not in (None, 0):
                change_amt = close - prev_close
                change_pct = (close - prev_close) / prev_close * 100.0
            volume = p.get("volume")
            trading_value = (close * volume) if (close is not None and volume is not None) else None
            try:
                conn.execute(
                    _UPSERT_PRICE_SQL,
                    (
                        t,
                        close,
                        change_pct,
                        change_amt,
                        prev_close,
                        p.get("open"),
                        p.get("high"),
                        p.get("low"),
                        volume,
                        trading_value,
                        now,
                    ),
                )
                priced += 1
            except Exception as e:
                print(f"  upsert 실패 {t}: {e}")
                _safe_commit(conn)
        _safe_commit(conn)
        print(f"[us-theme] 시세 진행: {min(i + CHUNK, len(tickers))}/{len(tickers)}  (성공 {priced})")

    print(f"[us-theme] 시세 완료: {priced}/{len(tickers)}건  ({time.time() - t0:.1f}s)")

    meta_done = 0
    if fill_meta:
        print(f"[us-theme] 메타데이터 수집 (name/sector/market_cap)...")
        t1 = time.time()
        for idx, t in enumerate(tickers, 1):
            meta = _fetch_meta(t)
            try:
                conn.execute(
                    _UPSERT_META_SQL,
                    (
                        t,
                        meta.get("name") or t,
                        meta.get("exchange"),
                        meta.get("sector"),
                        meta.get("industry"),
                        meta.get("market_cap"),
                        now,
                    ),
                )
                meta_done += 1
            except Exception as e:
                print(f"  meta 실패 {t}: {e}")
                _safe_commit(conn)
            if idx % 20 == 0:
                _safe_commit(conn)
                print(f"[us-theme] 메타 진행: {idx}/{len(tickers)}")
            if meta_sleep:
                time.sleep(meta_sleep)
        _safe_commit(conn)
        print(f"[us-theme] 메타 완료: {meta_done}건  ({time.time() - t1:.1f}s)")

    conn.close()
    return {"tickers": len(tickers), "priced": priced, "meta": meta_done}


if __name__ == "__main__":
    quick = "--quick" in sys.argv
    run(fill_meta=not quick)
