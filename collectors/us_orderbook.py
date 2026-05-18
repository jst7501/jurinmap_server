"""미국 주식 호가창 — yfinance Ticker.info 의 bid/ask + bidSize/askSize 활용.

KIS overseas 의 다중 호가(inquire-asking-price) endpoint 가 빈 output 반환 (권한·파라미터 이슈).
대안: yfinance 의 NBBO Top of Book (NASDAQ TotalView / NYSE OpenBook subset).

제공 데이터:
  bid / ask           — Best Bid / Best Ask 가격
  bidSize / askSize   — 각각 100주 단위 lot 수 (1=100주). NMS spec.
  spread              — ask - bid (절대값 + bp = (spread/mid)*10000)
  imbalance_ratio     — bidSize / (bidSize+askSize). 0.5=균형, 1.0=매수 절대 우위
  mid_price           — (bid+ask)/2
  market_state        — REGULAR / POST / PRE / CLOSED
  day_range_position  — (price - dayLow) / (dayHigh - dayLow). 0=저점, 1=고점

호가창은 Level 1 (Top of Book) 한정. Level 2 (Order Book Depth) 는 Polygon.io 등 유료 필요.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("collectors.us_orderbook")


def _safe_float(v) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _safe_int(v) -> Optional[int]:
    f = _safe_float(v)
    if f is None:
        return None
    return int(f)


def get_orderbook(symbol: str) -> dict:
    """단일 종목의 Top of Book 호가 + 매수/매도 우위 지표.

    Returns:
      {
        "symbol": "TSLA",
        "bid": 442.75, "ask": 445.08,
        "bid_size": 1, "ask_size": 1,    # 100주 단위 lot
        "bid_size_shares": 100,           # = bid_size * 100
        "ask_size_shares": 100,
        "mid_price": 443.92,
        "spread_abs": 2.33,
        "spread_bp": 52.5,                # basis points
        "imbalance_ratio": 0.5,           # 0~1, 0.5=균형
        "imbalance_label": "균형",         # "매수 우위" / "매도 우위" / "균형"
        "market_state": "POSTPOST",
        "regular_market_price": 445.27,
        "previous_close": 433.45,
        "day_low": 430.21, "day_high": 453.40,
        "day_range_position": 0.65,
        "volume": 64708180,
        "average_volume_10d": 57990950,
        "volume_ratio": 1.12,
        "as_of": "2026-05-14T..."
      }
    """
    try:
        import yfinance as yf
    except ImportError:
        raise RuntimeError("yfinance not installed")

    sym = (symbol or "").strip().upper()
    if not sym:
        raise ValueError("symbol required")

    # info (무거움) 와 fast_info (가벼움) 둘 다 시도 — small cap 페니는 info 비고 fast_info 만 있음
    info: dict = {}
    fast = None
    try:
        info = yf.Ticker(sym).info or {}
    except Exception:
        info = {}
    try:
        fast = yf.Ticker(sym).fast_info
    except Exception:
        fast = None

    bid = _safe_float(info.get("bid"))
    ask = _safe_float(info.get("ask"))
    bid_size = _safe_int(info.get("bidSize")) or 0
    ask_size = _safe_int(info.get("askSize")) or 0

    # bid/ask 둘 다 없으면 fast_info 의 가격으로 fallback (spread 0)
    if bid is None and ask is None:
        regular = (
            _safe_float(info.get("regularMarketPrice"))
            or _safe_float(info.get("currentPrice"))
            or (_safe_float(fast.last_price) if fast is not None else None)
        )
        if regular is not None:
            bid = ask = regular
        else:
            # bid/ask/현재가 모두 없어도 빈 dict 반환 — 502 raise 하지 않음
            from datetime import datetime, timezone
            return {
                "symbol": sym,
                "bid": None, "ask": None,
                "bid_size": 0, "ask_size": 0,
                "bid_size_shares": 0, "ask_size_shares": 0,
                "mid_price": None,
                "spread_abs": None, "spread_bp": None,
                "imbalance_ratio": 0.5,
                "imbalance_label": "데이터 없음",
                "market_state": info.get("marketState"),
                "regular_market_price": None,
                "previous_close": None,
                "open_price": None,
                "day_low": None, "day_high": None,
                "day_range_position": None,
                "volume": None,
                "average_volume_10d": None,
                "volume_ratio": None,
                "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "source": "yfinance_top_of_book",
                "note": "no_orderbook_data",
            }

    # 한 쪽만 있으면 다른 쪽을 동일 가격으로
    if bid is None and ask is not None:
        bid = ask
    elif ask is None and bid is not None:
        ask = bid

    # spread / mid
    spread_abs = round(ask - bid, 4) if ask is not None and bid is not None else None
    mid_price = round((ask + bid) / 2, 4) if ask is not None and bid is not None else None
    spread_bp = None
    if spread_abs is not None and mid_price and mid_price > 0:
        spread_bp = round(spread_abs / mid_price * 10000, 2)

    # imbalance ratio
    total_size = bid_size + ask_size
    if total_size > 0:
        imbalance_ratio = round(bid_size / total_size, 4)
    else:
        imbalance_ratio = 0.5  # 데이터 없음 = 균형

    # imbalance label (사용자 가독성)
    if imbalance_ratio >= 0.7:
        imbalance_label = "매수 강세"
    elif imbalance_ratio >= 0.58:
        imbalance_label = "매수 우위"
    elif imbalance_ratio >= 0.42:
        imbalance_label = "균형"
    elif imbalance_ratio >= 0.30:
        imbalance_label = "매도 우위"
    else:
        imbalance_label = "매도 강세"

    # 일중 위치
    day_low = _safe_float(info.get("dayLow"))
    day_high = _safe_float(info.get("dayHigh"))
    regular_price = _safe_float(info.get("regularMarketPrice")) or _safe_float(info.get("currentPrice"))
    day_range_position = None
    if day_low is not None and day_high is not None and day_high > day_low and regular_price is not None:
        day_range_position = round((regular_price - day_low) / (day_high - day_low), 4)
        day_range_position = max(0.0, min(1.0, day_range_position))

    # 거래량 ratio
    volume = _safe_int(info.get("volume")) or _safe_int(info.get("regularMarketVolume"))
    avg_volume = _safe_int(info.get("averageVolume10days")) or _safe_int(info.get("averageDailyVolume10Day"))
    volume_ratio = None
    if volume and avg_volume and avg_volume > 0:
        volume_ratio = round(volume / avg_volume, 2)

    from datetime import datetime, timezone
    as_of = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    return {
        "symbol": sym,
        "bid": bid,
        "ask": ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "bid_size_shares": bid_size * 100,   # NMS lot = 100주
        "ask_size_shares": ask_size * 100,
        "mid_price": mid_price,
        "spread_abs": spread_abs,
        "spread_bp": spread_bp,
        "imbalance_ratio": imbalance_ratio,
        "imbalance_label": imbalance_label,
        "market_state": info.get("marketState"),
        "regular_market_price": regular_price,
        "previous_close": _safe_float(info.get("previousClose")),
        "open_price": _safe_float(info.get("open")),
        "day_low": day_low,
        "day_high": day_high,
        "day_range_position": day_range_position,
        "volume": volume,
        "average_volume_10d": avg_volume,
        "volume_ratio": volume_ratio,
        "as_of": as_of,
        "source": "yfinance_top_of_book",
    }


if __name__ == "__main__":
    import json
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "TSLA"
    print(json.dumps(get_orderbook(sym), indent=2, default=str, ensure_ascii=False))
