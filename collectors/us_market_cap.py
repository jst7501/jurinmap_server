"""미국 종목 시가총액 + 현재가 — yfinance fast_info (가벼운 endpoint).

페니스탁 식별용: market_cap_usd < $100M = penny.

batch fetch:
  - ThreadPoolExecutor(workers=10) — 종목당 ~0.5s → 12K 종목 ~10분
  - fast_info 는 yfinance 의 가벼운 캐시 endpoint (Ticker.info 보다 10배 빠름)
  - 실패 종목은 (None, None) 반환

호출자가 직접 batch 분할 + 진행 표시 + DB upsert.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

logger = logging.getLogger("collectors.us_market_cap")

PENNY_CAP_THRESHOLD_USD = 100_000_000  # 시총 1억 달러 = 페니 컷오프


def fetch_one(symbol: str) -> tuple[str, Optional[float], Optional[float]]:
    """단일 ticker → (symbol, market_cap_usd, last_price). 실패 시 (sym, None, None)."""
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).fast_info
        mc = info.market_cap if info.market_cap and info.market_cap > 0 else None
        price = info.last_price if info.last_price and info.last_price > 0 else None
        return symbol, float(mc) if mc else None, float(price) if price else None
    except Exception:
        return symbol, None, None


def batch_fetch(
    symbols: list[str],
    workers: int = 10,
    progress_callback=None,
) -> dict[str, dict]:
    """N 종목 → {symbol: {market_cap, last_price}} dict.

    workers: 동시 worker 수. 너무 높으면 yfinance rate limit 받을 수 있음.
    progress_callback(i, total): 매 batch 진행 시 호출 (선택).
    """
    out: dict[str, dict] = {}
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_one, s): s for s in symbols}
        for fut in as_completed(futures):
            try:
                sym, mc, price = fut.result()
            except Exception:
                sym = futures[fut]
                mc, price = None, None
            out[sym] = {"market_cap_usd": mc, "last_price": price}
            completed += 1
            if progress_callback and completed % 50 == 0:
                progress_callback(completed, len(symbols))
    return out


def is_penny(market_cap_usd: Optional[float]) -> bool:
    """시총 100M USD 이하 = 페니."""
    if market_cap_usd is None:
        return False
    return market_cap_usd < PENNY_CAP_THRESHOLD_USD


if __name__ == "__main__":
    import sys
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    syms = sys.argv[1:] if len(sys.argv) > 1 else ["ALP", "WOK", "APLD", "COST", "NVDA", "AAPL"]
    import time
    t0 = time.time()
    res = batch_fetch(syms, workers=5)
    elapsed = time.time() - t0

    print(f"elapsed: {elapsed:.1f}s for {len(syms)} syms")
    for sym in syms:
        r = res.get(sym, {})
        mc = r.get("market_cap_usd")
        price = r.get("last_price")
        penny = " ← 페니" if is_penny(mc) else ""
        mc_str = f"${mc/1e6:.1f}M" if mc else "(없음)"
        price_str = f"${price:.2f}" if price else "(없음)"
        print(f"  {sym:6} cap={mc_str:12} price={price_str:10}{penny}")
