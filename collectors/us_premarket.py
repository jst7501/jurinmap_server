"""미국 pre-market / after-hours mover scanner.

yfinance batch download 으로 1분 봉 + prepost=True 가져온 뒤
세션별 (pre / regular / post) 최신 가격 + 전일 종가 대비 % 계산.

핵심 유즈케이스 — KST 22:00 ~ 23:30 (NY 8:00 ~ 9:30 pre-market):
  지금 가장 많이 오른 페니 종목 한 화면.

Session 판정:
  NY 04:00-09:30 = pre
  NY 09:30-16:00 = regular
  NY 16:00-20:00 = post

응답 형식:
  {
    "session": "pre" | "regular" | "post" | "closed",
    "ny_time": "2026-05-14 08:45:00 ET",
    "movers": [
      {"symbol": "ALP", "last": 2.41, "prev_close": 1.80,
       "change_pct": 33.9, "volume": 250000,
       "session": "pre", "as_of_utc": "..."},
      ...
    ]
  }
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from typing import Optional

logger = logging.getLogger("collectors.us_premarket")

# NY timezone — pytz 의존성 없이 UTC offset 만 사용 (DST 무시).
# yfinance 가 반환하는 timestamp 는 UTC-aware 라 이걸 그대로 .tz_convert("America/New_York") 가능.
# DST 정확성 위해선 pytz 필요. fallback 으로 UTC-4 (DST 시) / UTC-5 (표준시) 둘 다 시도.

try:
    import pytz
    _NY_TZ = pytz.timezone("America/New_York")
    _USE_PYTZ = True
except Exception:
    _NY_TZ = None
    _USE_PYTZ = False


def _to_ny(dt_utc: datetime) -> datetime:
    if _USE_PYTZ:
        return dt_utc.astimezone(_NY_TZ)
    # fallback — DST 자동 감지 어려움. 3월 둘째주 일 ~ 11월 첫주 일 EDT(UTC-4), 나머지 EST(UTC-5)
    y = dt_utc.year
    import calendar
    # DST 시작: 3월 둘째 일요일 02:00
    march = [d for d in range(8, 15) if datetime(y, 3, d).weekday() == 6]
    dst_start = datetime(y, 3, march[0], 2, 0)
    # DST 종료: 11월 첫째 일요일 02:00
    nov = [d for d in range(1, 8) if datetime(y, 11, d).weekday() == 6]
    dst_end = datetime(y, 11, nov[0], 2, 0)
    dt_local = dt_utc.replace(tzinfo=None)
    offset_hours = -4 if dst_start <= dt_local < dst_end else -5
    from datetime import timedelta
    return dt_utc + timedelta(hours=offset_hours)


def detect_session(now_utc: Optional[datetime] = None) -> str:
    """NY 현재 세션 판정. weekend → closed."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    ny = _to_ny(now_utc)
    if ny.weekday() >= 5:
        return "closed"
    t = ny.time()
    if time(4, 0) <= t < time(9, 30):
        return "pre"
    if time(9, 30) <= t < time(16, 0):
        return "regular"
    if time(16, 0) <= t < time(20, 0):
        return "post"
    return "closed"


def _classify_bar_session(bar_ts_ny) -> str:
    """단일 1분봉의 ny tz timestamp 가 어느 세션인지."""
    if bar_ts_ny.weekday() >= 5:
        return "closed"
    t = bar_ts_ny.time()
    if time(4, 0) <= t < time(9, 30):
        return "pre"
    if time(9, 30) <= t < time(16, 0):
        return "regular"
    if time(16, 0) <= t < time(20, 0):
        return "post"
    return "closed"


def fetch_premarket_movers(
    tickers: list[str],
    min_change_pct: float = 5.0,
    min_volume: int = 1000,
    limit: int = 50,
    session_filter: Optional[str] = None,
) -> dict:
    """yfinance batch download → 세션별 mover 추출.

    session_filter=None 이면 현재 세션 자동 사용. "pre"/"post"/"regular" 명시도 가능.
    """
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        logger.error("yfinance / pandas 필요")
        return {"session": "error", "movers": [], "error": "yfinance not installed"}

    if not tickers:
        return {"session": "empty", "movers": []}

    now_utc = datetime.now(timezone.utc)
    current_session = session_filter or detect_session(now_utc)
    if current_session == "closed":
        return {"session": "closed", "movers": [], "ny_time": _to_ny(now_utc).strftime("%Y-%m-%d %H:%M:%S ET")}

    # 1분봉 + prepost. period 5d 정도 가져와 전일 마감 비교 가능하게.
    syms = [s.upper() for s in tickers if s]
    try:
        df = yf.download(
            syms,
            period="5d",
            interval="1m",
            prepost=True,
            threads=True,
            progress=False,
            group_by="ticker",
            auto_adjust=False,
        )
    except Exception as exc:
        logger.error("yf.download fail: %s", exc)
        return {"session": current_session, "movers": [], "error": str(exc)}

    if df is None or df.empty:
        return {"session": current_session, "movers": []}

    movers: list[dict] = []
    for sym in syms:
        try:
            if len(syms) == 1:
                sub = df
            else:
                sub = df.get(sym)
            if sub is None or sub.empty:
                continue
            # ny tz convert
            try:
                sub = sub.tz_convert("America/New_York")
            except Exception:
                try:
                    sub = sub.tz_localize("UTC").tz_convert("America/New_York")
                except Exception:
                    pass

            # session 분류
            ny_index = sub.index
            sessions = [_classify_bar_session(ts) for ts in ny_index]
            sub = sub.copy()
            sub["_session"] = sessions

            # 가장 최근 current_session 봉
            current_bars = sub[sub["_session"] == current_session]
            if current_bars.empty:
                continue
            last_bar = current_bars.iloc[-1]
            last_price = float(last_bar.get("Close") or 0)
            if not last_price:
                continue

            # 가장 최근 regular session 마감 (전일 close)
            regular_bars = sub[sub["_session"] == "regular"]
            if regular_bars.empty:
                continue
            # 오늘이면 오늘 9:30 시작 전 / 어제 마감 사용
            # current_session=pre 이면 어제 16:00 종가
            # current_session=post 이면 오늘 16:00 종가 (방금 마감)
            today = ny_index[-1].date() if hasattr(ny_index[-1], "date") else None
            if current_session == "pre":
                # 어제 마감
                prev_regular = regular_bars[regular_bars.index.date < today] if today else regular_bars.iloc[:-1]
                if prev_regular.empty:
                    continue
                prev_close = float(prev_regular.iloc[-1].get("Close") or 0)
            elif current_session == "post":
                # 오늘 마감 (16:00 직전 마지막 바)
                today_regular = regular_bars[regular_bars.index.date == today] if today else regular_bars
                if today_regular.empty:
                    continue
                prev_close = float(today_regular.iloc[-1].get("Close") or 0)
            else:
                # current = regular: 어제 종가 vs 지금
                prev_regular = regular_bars[regular_bars.index.date < today] if today else regular_bars.iloc[:-1]
                if prev_regular.empty:
                    continue
                prev_close = float(prev_regular.iloc[-1].get("Close") or 0)

            if not prev_close:
                continue

            change_pct = (last_price - prev_close) / prev_close * 100

            # 세션 내 누적 거래량
            today_session_bars = current_bars[current_bars.index.date == today] if today else current_bars
            session_vol = int(today_session_bars.get("Volume").sum()) if not today_session_bars.empty else 0

            if abs(change_pct) < min_change_pct:
                continue
            if session_vol < min_volume:
                continue

            movers.append({
                "symbol": sym,
                "last": round(last_price, 4),
                "prev_close": round(prev_close, 4),
                "change_pct": round(change_pct, 2),
                "volume": session_vol,
                "session": current_session,
                "as_of_ny": last_bar.name.strftime("%Y-%m-%d %H:%M:%S ET"),
            })
        except Exception as exc:
            logger.debug("ticker %s err: %s", sym, exc)
            continue

    # 정렬: change_pct 절대값 내림차순
    movers.sort(key=lambda m: abs(m["change_pct"]), reverse=True)
    movers = movers[:limit]

    return {
        "session": current_session,
        "ny_time": _to_ny(now_utc).strftime("%Y-%m-%d %H:%M:%S ET"),
        "movers": movers,
        "scanned": len(syms),
        "matched": len(movers),
    }


if __name__ == "__main__":
    import sys
    import io as _io
    import json
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    syms = sys.argv[1:] if len(sys.argv) > 1 else ["AAPL", "NVDA", "TSLA", "AMD", "AMZN"]
    print(f"session: {detect_session()}")
    res = fetch_premarket_movers(syms, min_change_pct=0.1, min_volume=0, limit=20)
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
