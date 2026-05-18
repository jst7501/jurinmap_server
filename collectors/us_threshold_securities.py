"""NYSE Reg SHO Threshold Securities — 매일 EOD 발표.

Threshold Securities = Reg SHO Rule 203(b)(3) 위반 = days_to_cover 5일+ 누적된
종목. 거래량의 0.5% 이상 미결제 공매도가 5영업일 이상 지속 → 강제 buy-in 압력
누적. 이 list 진입 자체가 squeeze 후보 시그널.

데이터 소스:
  NYSE: https://www.nyse.com/api/regulatory/threshold-securities/download?selectedDate=YYYY-MM-DD&market={NYSE|NYSEArca|NYSEAmer|NYSENational|NYSEChicago}
    - 매일 EOD 직후 발표
    - txt 파이프 구분: Symbol|Security Name|Market Category|Reg SHO Threshold Flag|Filler|Filler
    - 빈 list 도 정상 (header + timestamp)
  NASDAQ: Incapsula 봇 차단으로 미사용. 후속에 다른 우회 필요.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

NYSE_MARKETS = ("NYSE", "NYSEArca", "NYSEAmer", "NYSENational", "NYSEChicago")
_URL_TEMPLATE = "https://www.nyse.com/api/regulatory/threshold-securities/download?selectedDate={date}&market={market}"


def _fetch_one(date_iso: str, market: str, timeout: int = 12) -> Optional[str]:
    url = _URL_TEMPLATE.format(date=date_iso, market=market)
    try:
        r = requests.get(url, headers=_HEADERS, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.text
    except Exception:
        return None


def parse(text: str, market: str) -> list[dict]:
    """파이프 구분 파싱. 빈 list 도 정상 — 일관성 위해 [] 반환.

    Format: Symbol|Security Name|Market Category|Reg SHO Threshold Flag|Filler|Filler
    헤더 1줄 + 데이터 N줄 + 마지막 timestamp 1줄.
    """
    out: list[dict] = []
    if not text:
        return out
    lines = text.replace("\r", "").split("\n")
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 헤더 / footer 스킵
        if line.startswith("Symbol|"):
            continue
        if line.replace(" ", "").isdigit():  # YYYYMMDDHHMMSS footer
            continue
        parts = line.split("|")
        if len(parts) < 4:
            continue
        sym = parts[0].strip()
        name = parts[1].strip()
        cat = parts[2].strip()
        flag = parts[3].strip().upper()
        if not sym or flag != "Y":
            continue
        out.append({
            "symbol": sym,
            "name": name,
            "market_category": cat,
            "market": market,
            "is_threshold": True,
        })
    return out


def fetch_threshold_date(date_iso: str) -> list[dict]:
    """특정 일자에 NYSE 산하 모든 market 의 threshold securities 모아서 반환.
    중복 종목은 제거. 빈 list 도 정상.
    """
    seen: dict[str, dict] = {}
    for market in NYSE_MARKETS:
        text = _fetch_one(date_iso, market)
        if not text:
            continue
        for row in parse(text, market):
            sym = row["symbol"]
            if sym not in seen:
                seen[sym] = row
    return list(seen.values())


def fetch_latest_available(lookback_days: int = 7) -> tuple[Optional[str], list[dict]]:
    """가장 최근 영업일 (= 데이터 있는 날) 부터 lookback. NYSE 발표 직후 KST 새벽.

    Returns: (date_iso, list_of_rows). 빈 list 일 수도 있고 (그 날 threshold 종목 0개), None 일 수도 (fetch 실패).
    """
    today_et = datetime.now(timezone.utc) - timedelta(hours=4)
    for d in range(lookback_days):
        date_iso = (today_et - timedelta(days=d)).strftime("%Y-%m-%d")
        rows = fetch_threshold_date(date_iso)
        # rows 가 빈 list 라도 우리는 fetch 성공으로 간주 — 그 날 0개 일 수도 있으니
        # 단, 응답 자체가 다 None 이었다면 fetch_one 다 실패 → 다음 날로
        any_response = any(_fetch_one(date_iso, m) for m in NYSE_MARKETS[:1])
        if not any_response:
            continue
        return date_iso, rows
    return None, []


if __name__ == "__main__":
    # 최근 일자 시도
    date_iso, rows = fetch_latest_available()
    print(f"as of: {date_iso}  count: {len(rows)}")
    for r in rows[:20]:
        print(f"  {r['symbol']:<6} {r['market']:<15} {r['market_category']:<20} {r['name'][:60]}")
