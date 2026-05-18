"""미국 페니 펌프&덤프 패턴 분석.

종목별 과거 1-2년 일봉 데이터에서:
  1. 급등 이벤트 추출 (가격 +30%↑ OR 거래량 평소 5배+ + 가격 +15%↑)
  2. 각 이벤트 후 +1/+3/+7/+30일 가격 추적
  3. 펌프 정점 대비 회수율 (peak retracement) 측정
  4. 통계: "이전 N번 급등 → 평균 7일 후 -X% / 30일 후 -Y%"

페니의 전형 펌프&덤프:
  Day 0:  +50% 펌프 발생 (catalyst: news, S-1, 트윗 등)
  Day +1: +20% 추가 (FOMO 진입)
  Day +3: -30% 시작 (early seller)
  Day +7: -60% (full reversion)
  Day +30: -80% 또는 base 회귀

핵심 메트릭:
  spike_pct        — 그날 +%
  volume_ratio     — 평소 거래량 대비
  peak_day_offset  — 펌프 정점이 몇 일 후
  peak_pct         — 정점 가격 / spike 시작가
  d_plus_7_pct     — 7일 후 spike 시작가 대비
  d_plus_30_pct    — 30일 후 spike 시작가 대비
  base_recovery    — 30일 후 가격이 spike 발생 전 20일 평균까지 회귀했는지
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("collectors.us_pump_dump")


def fetch_pump_dump_events(
    symbol: str,
    days: int = 365,
    spike_threshold_pct: float = 30.0,
    volume_multiplier: float = 5.0,
    min_volume_spike_pct: float = 15.0,
    preloaded_bars: Optional[list] = None,
) -> Optional[dict]:
    """일봉 데이터로 급등 이벤트 + 후속 결과 추출.

    preloaded_bars: DB 캐시 [{date,open,high,low,close,volume}] — 제공 시 yfinance 호출 생략.
    spike 정의:
      A) 단일 일봉 +spike_threshold_pct% 이상 OR
      B) 거래량 평소(20일 평균)의 volume_multiplier+배 + 가격 +min_volume_spike_pct% 이상

    인접 한 일 안에 연속 spike 면 첫 번째만 카운트 (중복 제거).
    """
    try:
        import pandas as pd
    except ImportError:
        return None
    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    if preloaded_bars and len(preloaded_bars) >= 30:
        # DB 캐시 사용 — yfinance 호출 없음
        import pandas as pd
        df = pd.DataFrame(preloaded_bars)
        df = df.rename(columns={
            "date": "Date", "open": "Open", "high": "High",
            "low": "Low", "close": "Close", "volume": "Volume",
        })
        df.index = pd.to_datetime(df["Date"])
        hist = df
    else:
        try:
            import yfinance as yf
            hist = yf.Ticker(sym).history(period=f"{days + 60}d", interval="1d", auto_adjust=False)
        except Exception as exc:
            logger.debug("yfinance fetch %s: %s", sym, exc)
            return None

    if hist is None or len(hist) < 30:
        return None

    # pandas 컬럼: Open, High, Low, Close, Volume
    closes = hist["Close"].values
    volumes = hist["Volume"].values
    dates = [idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10] for idx in hist.index]
    highs = hist["High"].values

    n = len(closes)
    if n < 30:
        return None

    # 20일 이동 평균 거래량
    avg_vol_20 = []
    for i in range(n):
        start = max(0, i - 20)
        sub = volumes[start:i] if i > 0 else volumes[0:1]
        sub_valid = [v for v in sub if v > 0]
        avg_vol_20.append(sum(sub_valid) / len(sub_valid) if sub_valid else 1)

    events = []
    last_event_idx = -10  # 인접 spike 중복 제거 (10일 이내 중복 무시)

    for i in range(1, n):
        prev = closes[i - 1]
        cur = closes[i]
        if prev <= 0:
            continue
        change_pct = (cur - prev) / prev * 100
        vol = volumes[i] if volumes[i] else 0
        avg_v = avg_vol_20[i] if avg_vol_20[i] > 0 else 1
        vol_ratio = vol / avg_v if avg_v > 0 else 0

        is_big_spike = change_pct >= spike_threshold_pct
        is_volume_spike = vol_ratio >= volume_multiplier and change_pct >= min_volume_spike_pct
        if not (is_big_spike or is_volume_spike):
            continue
        if i - last_event_idx < 10:
            continue
        last_event_idx = i

        # 후속 가격 추적 — +1, +3, +7, +30일
        def _close_at_offset(offset):
            idx = i + offset
            if idx < n:
                return float(closes[idx]) if closes[idx] > 0 else None
            return None

        spike_start = float(prev)  # 펌프 직전 종가 (anchor)
        spike_day_close = float(cur)

        # peak — i+1 ~ i+10 중 최고가 (High 기준)
        peak_idx = i
        peak_price = float(highs[i]) if highs[i] > 0 else spike_day_close
        for j in range(i + 1, min(n, i + 11)):
            if highs[j] > peak_price:
                peak_price = float(highs[j])
                peak_idx = j

        d_plus_1 = _close_at_offset(1)
        d_plus_3 = _close_at_offset(3)
        d_plus_7 = _close_at_offset(7)
        d_plus_30 = _close_at_offset(30)

        def _pct(then, base):
            if then is None or base is None or base <= 0:
                return None
            return round((then - base) / base * 100, 2)

        # 펌프 전 base — 직전 20일 평균
        base_start = max(0, i - 20)
        prev_20 = [c for c in closes[base_start:i] if c > 0]
        base_avg = sum(prev_20) / len(prev_20) if prev_20 else None

        events.append({
            "date": dates[i],
            "change_pct": round(change_pct, 2),
            "close": spike_day_close,
            "spike_anchor": spike_start,
            "volume": int(vol),
            "volume_ratio_20d": round(vol_ratio, 1),
            "trigger": "big_spike" if is_big_spike else "volume_spike",
            "peak_price": round(peak_price, 4),
            "peak_day_offset": peak_idx - i,
            "peak_vs_anchor_pct": _pct(peak_price, spike_start),
            "d_plus_1_pct": _pct(d_plus_1, spike_start),
            "d_plus_3_pct": _pct(d_plus_3, spike_start),
            "d_plus_7_pct": _pct(d_plus_7, spike_start),
            "d_plus_30_pct": _pct(d_plus_30, spike_start),
            "base_avg_20d_before": round(base_avg, 4) if base_avg else None,
            "base_recovery": (d_plus_30 is not None and base_avg is not None and d_plus_30 <= base_avg * 1.1) if (d_plus_30 and base_avg) else None,
        })

    # 통계 계산
    def _bucket(returns):
        valid = [r for r in returns if r is not None]
        if not valid:
            return None
        up = sum(1 for r in valid if r > 0)
        down = sum(1 for r in valid if r < 0)
        avg = sum(valid) / len(valid)
        median = sorted(valid)[len(valid) // 2]
        worst = min(valid)
        return {
            "count": len(valid),
            "up": up,
            "down": down,
            "avg_pct": round(avg, 2),
            "median_pct": round(median, 2),
            "worst_pct": round(worst, 2),
        }

    stats = {
        "d_plus_1": _bucket([e["d_plus_1_pct"] for e in events]),
        "d_plus_3": _bucket([e["d_plus_3_pct"] for e in events]),
        "d_plus_7": _bucket([e["d_plus_7_pct"] for e in events]),
        "d_plus_30": _bucket([e["d_plus_30_pct"] for e in events]),
        "peak_offset_avg": round(sum(e["peak_day_offset"] for e in events) / len(events), 1) if events else None,
        "base_recovery_count": sum(1 for e in events if e.get("base_recovery") is True),
        "base_recovery_unknown": sum(1 for e in events if e.get("base_recovery") is None),
    }

    return {
        "symbol": sym,
        "period_days": days,
        "total_events": len(events),
        "events": list(reversed(events)),  # 최신 → 과거
        "stats": stats,
        "thresholds": {
            "spike_threshold_pct": spike_threshold_pct,
            "volume_multiplier": volume_multiplier,
            "min_volume_spike_pct": min_volume_spike_pct,
        },
    }


if __name__ == "__main__":
    import sys, io as _io, json
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    syms = sys.argv[1:] if len(sys.argv) > 1 else ["ALP", "NVDA"]
    for sym in syms:
        res = fetch_pump_dump_events(sym, days=365)
        if not res:
            print(f"\n{sym}: (no data)")
            continue
        print(f"\n=== {sym} — {res['total_events']} pump events in {res['period_days']}일 ===")
        if res["stats"]["d_plus_30"]:
            s = res["stats"]["d_plus_30"]
            print(f"  30일 후 평균: {s['avg_pct']}% (중앙값 {s['median_pct']}%) · {s['down']}/{s['count']} 하락")
        for e in res["events"][:10]:
            print(f"  {e['date']} : +{e['change_pct']}% · 거래량 x{e['volume_ratio_20d']} · "
                  f"peak +{e['peak_vs_anchor_pct']}% (D+{e['peak_day_offset']}) · "
                  f"D+7 {e['d_plus_7_pct']}% · D+30 {e['d_plus_30_pct']}%")
