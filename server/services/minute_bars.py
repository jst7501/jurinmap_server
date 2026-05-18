"""1분봉 수집·집계·점수 헬퍼.

KIS REST inquire-time-itemchartprice 호출 + 시간 단위 집계 + 단타 적합도
스코어. 원래 server/routes/stocks_parts/part01_realtime_base.py 에 있던 코드를
services 로 분리. part01 이 이 함수들을 import 해서 stocks_parts 공유
네임스페이스에 노출하므로 part03/04 같은 다른 part 파일도 변경 없이 사용 가능.
"""

from collections import defaultdict
from datetime import datetime, timedelta


def _time_sub(hhmmss: str, minutes: int) -> str:
    """HHMMSS 에서 minutes 분 빼기. 09:00 이전이면 '090000' 반환."""
    h, m, s = int(hhmmss[:2]), int(hhmmss[2:4]), int(hhmmss[4:6])
    total = h * 60 + m - minutes
    if total <= 540:  # 09:00 = 540분
        return "090000"
    return f"{total // 60:02d}{total % 60:02d}{s:02d}"


def _fetch_full_day_minutes(collector, code: str) -> list:
    """당일 장 전체(09:00~현재) 1분봉 수집. 최대 15회 KIS API 호출."""
    query_time = datetime.now().strftime("%H%M%S")
    path = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
    seen: set = set()
    all_candles: list = []

    for _ in range(15):  # 하루 최대 13회면 충분, 안전 상한 15
        try:
            res = collector._get(path, {
                "FID_ETC_CLS_CODE": "",
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": code,
                "FID_INPUT_HOUR_1": query_time,
                "FID_PW_DATA_INCU_YN": "Y",
            }, "FHKST03010200")
        except Exception:
            break

        raw = res.get("output2") or []
        if isinstance(raw, dict):
            raw = [raw]
        if not raw:
            break

        batch = []
        for r in raw:
            t = r.get("stck_cntg_hour", "")
            if not t or t in seen:
                continue
            seen.add(t)
            close_v = int(r.get("stck_prpr") or 0)
            open_v = int(r.get("stck_oprc") or 0)
            if close_v == 0 and open_v == 0:
                continue
            batch.append({
                "time": t,
                "open": open_v,
                "high": int(r.get("stck_hgpr") or 0),
                "low": int(r.get("stck_lwpr") or 0),
                "close": close_v,
                "volume": int(r.get("cntg_vol") or 0),
            })

        if not batch:
            break
        all_candles.extend(batch)

        earliest = min(c["time"] for c in batch)
        if earliest <= "090100":  # 장 시작(09:01) 도달
            break
        query_time = _time_sub(earliest, 1)

    return sorted(all_candles, key=lambda c: c["time"])


def _fetch_multiday_minutes(collector, code: str, days: int = 3) -> list:
    """과거 N일 + 당일 1분봉 수집. 날짜별로 KIS API 호출."""
    import time as _time

    all_candles = []
    today = datetime.now()

    for d in range(days - 1, -1, -1):
        target = today - timedelta(days=d)
        date_str = target.strftime("%Y%m%d")
        weekday = target.weekday()
        if weekday >= 5:  # 토/일 스킵
            continue

        query_time = "153000" if d > 0 else today.strftime("%H%M%S")
        seen = set()

        for _ in range(15):
            try:
                params = {
                    "FID_ETC_CLS_CODE": "",
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": code,
                    "FID_INPUT_HOUR_1": query_time,
                    "FID_PW_DATA_INCU_YN": "Y",
                }
                if d > 0:
                    params["FID_INPUT_DATE_1"] = date_str
                res = collector._get(
                    "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
                    params, "FHKST03010200"
                )
            except Exception:
                break

            raw = res.get("output2") or []
            if isinstance(raw, dict):
                raw = [raw]
            if not raw:
                break

            batch = []
            for r in raw:
                t = r.get("stck_cntg_hour", "")
                key = f"{date_str}_{t}"
                if not t or key in seen:
                    continue
                seen.add(key)
                close_v = int(r.get("stck_prpr") or 0)
                open_v = int(r.get("stck_oprc") or 0)
                if close_v == 0 and open_v == 0:
                    continue
                batch.append({
                    "date": date_str,
                    "time": t,
                    "open": open_v,
                    "high": int(r.get("stck_hgpr") or 0),
                    "low": int(r.get("stck_lwpr") or 0),
                    "close": close_v,
                    "volume": int(r.get("cntg_vol") or 0),
                })

            if not batch:
                break
            all_candles.extend(batch)
            earliest = min(c["time"] for c in batch)
            if earliest <= "090100":
                break
            query_time = _time_sub(earliest, 1)

        _time.sleep(0.15)  # KIS rate limit

    return sorted(all_candles, key=lambda c: (c.get("date", ""), c["time"]))


def _aggregate_to_30min(minute_candles: list) -> list:
    """1분봉 리스트를 30분봉으로 집계. 시간 오름차순 OHLCV 반환."""
    return _aggregate_to_Nmin(minute_candles, 30)


def _aggregate_to_Nmin(minute_candles: list, n: int) -> list:
    """1분봉 리스트를 N분봉으로 집계. 날짜+시간 기준 오름차순 OHLCV 반환."""
    if n <= 1:
        return minute_candles
    buckets: dict = defaultdict(list)
    for c in minute_candles:
        t = c["time"]
        d = c.get("date", "")
        h, m = int(t[:2]), int(t[2:4])
        time_key = f"{h:02d}{(m // n) * n:02d}00"
        key = f"{d}_{time_key}" if d else time_key
        buckets[key].append(c)

    result = []
    for key in sorted(buckets):
        g = sorted(buckets[key], key=lambda c: c["time"])
        result.append({
            "date": g[0].get("date", ""),
            "time": g[0]["time"][:4] + "00",
            "open": g[0]["open"],
            "high": max(c["high"] for c in g),
            "low": min(c["low"] for c in g),
            "close": g[-1]["close"],
            "volume": sum(c["volume"] for c in g),
        })
    return result


def _calc_scalping_index(candles: list) -> dict:
    """단타 적합도 점수.

    candles: 최신순 list of {time, open, high, low, close, volume}
    returns: {score, label, detail}
    """
    if len(candles) < 5:
        return {"score": 0, "label": "데이터 부족", "detail": {}}

    candles = list(reversed(candles))  # 오래된 → 최신 순서로

    # ── 1. 변동성 점수 (0-35) ─────────────────────────────────
    ranges = []
    for c in candles[-20:]:
        if c["close"] and c["close"] > 0:
            r = (c["high"] - c["low"]) / c["close"] * 100
            ranges.append(r)
    avg_range = sum(ranges) / len(ranges) if ranges else 0

    if avg_range < 0.2:
        vol_score = 5
    elif avg_range < 0.5:
        vol_score = int(avg_range / 0.5 * 20) + 5
    elif avg_range < 1.2:
        vol_score = int((avg_range - 0.5) / 0.7 * 15) + 25
    else:
        vol_score = max(20, 35 - int((avg_range - 1.2) * 10))

    # ── 2. 거래량 폭발 점수 (0-35) ────────────────────────────
    recent_vols = [c["volume"] for c in candles[-5:] if c["volume"]]
    base_vols = [c["volume"] for c in candles[-25:-5] if c["volume"]]
    avg_recent = sum(recent_vols) / len(recent_vols) if recent_vols else 0
    avg_base = sum(base_vols) / len(base_vols) if base_vols else 1
    vol_ratio = avg_recent / avg_base if avg_base > 0 else 1.0

    if vol_ratio >= 3.0:
        volume_score = 35
    elif vol_ratio >= 2.0:
        volume_score = 28
    elif vol_ratio >= 1.3:
        volume_score = 20
    elif vol_ratio >= 0.8:
        volume_score = 12
    else:
        volume_score = 5

    # ── 3. 모멘텀 점수 (0-30) ────────────────────────────────
    recent = candles[-10:]
    green = sum(1 for c in recent if c["close"] >= c["open"])
    red = len(recent) - green
    price_change_pct = 0.0
    if len(candles) >= 10 and candles[-10]["close"] > 0:
        price_change_pct = (candles[-1]["close"] - candles[-10]["close"]) / candles[-10]["close"] * 100

    if green >= 8:
        momentum_score = 30
    elif green >= 6:
        momentum_score = 22
    elif green >= 5:
        momentum_score = 15
    elif red >= 7:
        momentum_score = 8
    else:
        momentum_score = 10

    # ── 최종 점수 ─────────────────────────────────────────────
    score = vol_score + volume_score + momentum_score

    if score >= 75:
        label = "단타 최적"
    elif score >= 55:
        label = "단타 양호"
    elif score >= 35:
        label = "보통"
    else:
        label = "단타 어려움"

    return {
        "score": score,
        "label": label,
        "detail": {
            "volatility_score": vol_score,
            "volume_score": volume_score,
            "momentum_score": momentum_score,
            "avg_range_pct": round(avg_range, 3),
            "volume_ratio": round(vol_ratio, 2),
            "green_candles": green,
            "price_change_10m_pct": round(price_change_pct, 2),
            "candle_count": len(candles),
        },
    }
