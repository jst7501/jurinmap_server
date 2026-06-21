"""종목 연재(serial narrative) 감지 — stock_daily_summary 시계열에서
'이어지는 이야기'를 찾아 streak + 누적 수익률 + 타임라인으로 구성한다.

별도 수집 없음. 이미 매일 쌓이는 stock_daily_summary(code,summary_date,one_liner,
drivers_json,tone) 의 history 를 재조립한다. on-demand 로 종목 1개씩 호출.

핵심 아이디어:
- drivers/one_liner 의 자유 텍스트를 canonical 수급 태그로 정규화
  (예: '외국인 매수전환' '외국인 5일 순매수' '외국인기관매수' → '외국인 매수')
- 가장 최근 날부터 연속으로 이어지는 태그 = 현재 연재 스토리라인(anchor)
- 그 streak 시작일 종가 대비 현재 종가로 누적 수익률 계산
"""
from __future__ import annotations

import json as _json
from typing import Any


# canonical 태그 → 판정 규칙. (포함 키워드, 제외 키워드)
# 순서 = 우선순위 (위에서 먼저 매칭). 수급 스레드가 연재 가치 최상.
_TAG_RULES: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    ("외국인 매도", ("외국인", "외인"), ()),   # 매도/매수는 아래서 방향 결정
    ("기관 매도", ("기관",), ()),
    ("개인 매수", ("개인",), ()),
    ("공매도 부담", ("공매도",), ()),
    ("신고가", ("신고가",), ()),
    ("실적 기대", ("실적", "어닝", "가이던스"), ()),
]

_BUY_WORDS = ("매수", "매집", "순매수", "매수전환", "담", "사들", "유입")
_SELL_WORDS = ("매도", "순매도", "던", "차익", "이탈")


def _norm_tags(drivers: list[str], one_liner: str) -> set[str]:
    """하루치 drivers + one_liner 를 canonical 태그 집합으로."""
    blob = " ".join(drivers) + " " + (one_liner or "")
    tags: set[str] = set()

    # 수급 3주체 — 방향(매수/매도) 판정
    for actor, keys in (("외국인", ("외국인", "외인")),
                        ("기관", ("기관",)),
                        ("개인", ("개인",))):
        # 해당 주체가 drivers 안에 명시됐을 때만 (one_liner 전체 스캔은 노이즈)
        actor_in_drivers = any(any(k in d for k in keys) for d in drivers)
        if not actor_in_drivers:
            continue
        # 그 주체 토큰 주변 방향어로 매수/매도 결정
        seg = " ".join(d for d in drivers if any(k in d for k in keys))
        buy = any(w in seg for w in _BUY_WORDS)
        sell = any(w in seg for w in _SELL_WORDS)
        if buy and not sell:
            tags.add(f"{actor} 매수")
        elif sell and not buy:
            tags.add(f"{actor} 매도")

    # 단순 키워드 태그
    if "공매도" in blob:
        tags.add("공매도 부담")
    if "신고가" in blob:
        tags.add("신고가")
    if any(w in blob for w in ("실적", "어닝", "가이던스")):
        tags.add("실적 기대")
    return tags


def _dkey(date: str) -> str:
    """날짜 문자열을 숫자만 남긴 canonical key 로 (20260619)."""
    return "".join(ch for ch in (date or "") if ch.isdigit())


def build_storyline(rows: list[dict], price_rows: list[dict]) -> dict:
    """
    rows: stock_daily_summary 최근→과거 [{summary_date, one_liner, drivers, tone}]
          (호출자가 drivers_json 을 list 로 파싱해 전달)
    price_rows: price_daily [{date, close}] (오름차순)
    """
    if not rows:
        return {"available": False}

    # 날짜 오름차순으로 (streak 계산 편의)
    days = sorted(rows, key=lambda r: r["summary_date"])
    # price 는 날짜 포맷이 다를 수 있어(20260619 vs 2026-06-19) canonical key 로 정규화
    prices = {_dkey(p["date"]): p["close"] for p in price_rows if p.get("close")}
    sorted_price_dates = sorted(prices.keys())

    def _prev_close(date_key: str) -> int | None:
        for pd in reversed([x for x in sorted_price_dates if x < date_key]):
            return prices[pd]
        return None

    # 각 날짜 태그 + 전일대비 등락률
    timeline: list[dict] = []
    for d in days:
        date = d["summary_date"]
        dk = _dkey(date)
        close = prices.get(dk)
        pc = _prev_close(dk)
        cp = round((close - pc) / pc * 100, 2) if close and pc else None
        tags = _norm_tags(d.get("drivers") or [], d.get("one_liner") or "")
        timeline.append({
            "date": date,
            "tone": d.get("tone") or "",
            "change_pct": cp,
            "one_liner": d.get("one_liner") or "",
            "tags": sorted(tags),
        })

    # 현재 연재 anchor = 마지막 날 태그 중, 뒤에서부터 연속 등장 streak 가장 긴 것
    last_tags = set(timeline[-1]["tags"])
    best_anchor = None
    best_streak = 0
    best_start_idx = len(timeline) - 1
    for tag in last_tags:
        streak = 0
        start_idx = len(timeline) - 1
        for i in range(len(timeline) - 1, -1, -1):
            if tag in timeline[i]["tags"]:
                streak += 1
                start_idx = i
            else:
                break
        if streak > best_streak:
            best_streak = streak
            best_anchor = tag
            best_start_idx = start_idx

    storyline = None
    if best_anchor and best_streak >= 2:  # 2일 이상 이어져야 '연재'
        start_date = timeline[best_start_idx]["date"]
        # 누적 수익률: streak 시작일 직전 종가 → 최신 종가
        start_prev = _prev_close(_dkey(start_date))
        latest_close = prices.get(_dkey(timeline[-1]["date"]))
        since_pct = None
        if start_prev and latest_close:
            since_pct = round((latest_close - start_prev) / start_prev * 100, 2)
        storyline = {
            "anchor": best_anchor,
            "streak_days": best_streak,
            "start_date": start_date,
            "since_pct": since_pct,
        }

    return {
        "available": True,
        "storyline": storyline,
        "timeline": timeline,
    }
