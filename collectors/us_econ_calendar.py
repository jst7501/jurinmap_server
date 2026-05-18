"""미국 매크로 이벤트 캘린더 — Trading Economics 페이지 스크래핑.

한국 아침 시황 brief 작성 시 "전날 미국에서 PPI/CPI/Fed 발언/소매판매 같은
이벤트가 있었는지" 컨텍스트로 쓰기 위함. 사용자 피드백:
  "아침 브리핑에서 전날 미국에서 PPI 있었을 수도 있고 특별한 이벤트들이
   있었을 수도 있는데 그거에 대해서 안 다루네."

데이터 소스: https://tradingeconomics.com/united-states/calendar
  - actual / forecast / consensus / previous / revised 까지 SSR HTML
  - 영문이라 Naver 뉴스 한 번 더 보강하면 좋지만, 이 collector 단독으로도
    "이벤트 명·결과·예상 대비 surprise" 정보는 충분.

다른 collector 들과 일관성 위해 dict 반환. 실패 시 빈 list.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_URL = "https://tradingeconomics.com/united-states/calendar"

# Trading Economics 의 별 표시 importance — date td 안 span 의 class 에 박혀있다.
#   "calendar-date-3" → High   (3 stars, 빨강 캘린더 아이콘)
#   "calendar-date-2" → Medium (2 stars)
#   "calendar-date-1" → Low    (1 star)
_IMPORTANCE_PATTERNS = [
    (re.compile(r"calendar-date-3\b", re.I), "high"),
    (re.compile(r"calendar-date-2\b", re.I), "medium"),
    (re.compile(r"calendar-date-1\b", re.I), "low"),
]


def _parse_date_class(td_class_str: str) -> Optional[str]:
    """date td의 class에 'YYYY-MM-DD' 가 박혀있다. 그걸 추출."""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", td_class_str)
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def _to_kst(date_str: str, time_str: str) -> Optional[str]:
    """ET (Eastern Time) → KST 변환. 'YYYY-MM-DD'+'12:30 PM' → 'YYYY-MM-DD HH:MM'.

    DST 정확 처리는 안 함 (-4 또는 -5 차이). 5월 기준 EDT (-4), 11월~3월은 EST (-5).
    Naive 한 +13 또는 +14 시차 적용. 일/년 단위 정확성 위해 datetime 사용.
    """
    if not date_str or not time_str:
        return None
    t = time_str.strip()
    # "All Day" 또는 비어있으면 시간 없는 이벤트
    if not t or t.lower().startswith("all"):
        return f"{date_str}"
    # "12:30 PM" / "12:30PM" / "12:30" 패턴
    m = re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", t, re.I)
    if not m:
        return None
    hh, mm, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").upper()
    if ampm == "PM" and hh != 12:
        hh += 12
    elif ampm == "AM" and hh == 12:
        hh = 0
    try:
        # 5월 = EDT(-4). 4~10월 -4, 그 외 -5 단순 휴리스틱.
        y, mo, d = map(int, date_str.split("-"))
        et_offset = -4 if 3 <= mo <= 10 else -5
        et = datetime(y, mo, d, hh, mm, tzinfo=timezone(timedelta(hours=et_offset)))
        kst = et.astimezone(timezone(timedelta(hours=9)))
        return kst.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


def _surprise_label(actual: Optional[str], forecast: Optional[str]) -> Optional[str]:
    """실제 vs 예상 비교 라벨. % 또는 K/B 단위는 단순 숫자만 추출.

    반환: "above" (예상 상회) / "in_line" / "below" (하회) / None (비교 불가)
    """
    def _num(x):
        if not x:
            return None
        m = re.search(r"-?\d+(?:\.\d+)?", str(x))
        return float(m.group()) if m else None

    a = _num(actual)
    f = _num(forecast)
    if a is None or f is None:
        return None
    diff = a - f
    eps = abs(f) * 0.02 if f else 0.01  # 2% 이내 → in_line
    if abs(diff) <= eps:
        return "in_line"
    return "above" if diff > 0 else "below"


def fetch_us_calendar_html(timeout: int = 15) -> Optional[str]:
    """Trading Economics 미국 캘린더 페이지 fetch. 실패 시 None."""
    try:
        r = requests.get(_URL, headers=_HEADERS, timeout=timeout)
        if r.status_code != 200 or "calendar-event" not in r.text:
            return None
        return r.text
    except Exception:
        return None


def parse_events(html: str) -> list[dict]:
    """캘린더 페이지 HTML 에서 이벤트 row 들 추출."""
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("tr[data-event]")
    out: list[dict] = []
    for tr in rows:
        country = (tr.get("data-country") or "").strip().lower()
        if country and country != "united states":
            # 페이지 자체가 /united-states/calendar 이라 거의 다 미국이지만 방어.
            continue
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 4:
            continue
        # 1) 날짜·시간
        date_td = tds[0]
        date_str = _parse_date_class(date_td.get("class") and " ".join(date_td.get("class")) or "")
        time_str = ""
        time_span = date_td.find("span", class_=re.compile(r"event-24|calendar-date"))
        if time_span:
            time_str = time_span.get_text(" ", strip=True)
        # 2) 이벤트명·참조 기간
        title_a = tr.select_one("a.calendar-event")
        title = title_a.get_text(strip=True) if title_a else (tr.get("data-event") or "").strip()
        ref_span = tr.select_one("span.calendar-reference")
        reference = ref_span.get_text(strip=True) if ref_span else ""
        # 3) actual / consensus / forecast / previous
        actual_el = tr.find(id="actual")
        actual = actual_el.get_text(strip=True) if actual_el else ""
        previous_el = tr.find(id="previous")
        previous = previous_el.get_text(strip=True) if previous_el else ""
        consensus_el = tr.find(id="consensus")
        consensus = consensus_el.get_text(strip=True) if consensus_el else ""
        forecast_el = tr.find(id="forecast")
        forecast = forecast_el.get_text(strip=True) if forecast_el else ""
        # consensus 가 비어있고 forecast 만 있는 경우 등 폴백
        compare_target = consensus or forecast
        # 4) importance — date td 안의 span class 에서 calendar-date-N 추출 (N=1/2/3)
        importance = None
        date_span = date_td.find("span", class_=re.compile(r"calendar-date-"))
        cls_str = " ".join(date_span.get("class") or []) if date_span else ""
        for pat, level in _IMPORTANCE_PATTERNS:
            if pat.search(cls_str):
                importance = level
                break
        # 5) revised 메모 (이전치 수정)
        revised_el = tr.find(id="revised")
        revised_from = None
        if revised_el:
            title_attr = revised_el.get("title") or ""
            m = re.search(r"revised from ([\d.\-%KMBT]+)", title_attr, re.I)
            if m:
                revised_from = m.group(1)

        out.append({
            "date": date_str,
            "time_et": time_str,
            "kst": _to_kst(date_str, time_str),
            "title": title,
            "reference": reference,        # 예: "APR" (참조 기간)
            "symbol": (tr.get("data-symbol") or "").strip() or None,
            "actual": actual or None,
            "forecast": forecast or consensus or None,
            "consensus": consensus or None,
            "previous": previous or None,
            "previous_revised_from": revised_from,
            "importance": importance,
            "surprise": _surprise_label(actual, compare_target),
        })
    return out


def get_us_overnight_events(
    lookback_hours: int = 30,
    min_importance: str = "medium",
    timeout: int = 15,
) -> list[dict]:
    """한국 시황 brief 시점 기준 직전 N시간 내에 발표된 미국 매크로 이벤트만 반환.

    Args:
        lookback_hours: 현재 KST 시각 기준 몇 시간 전까지 포함할지. 기본 30h
                        — 한국 아침 7시 brief 기준 이전 24h 미국 ET 거의 다 커버.
        min_importance: 'high' / 'medium' / 'low'. 'medium' 이면 high+medium 만.
                        None 이면 모두 포함.
        timeout: HTTP 타임아웃.

    Returns:
        list of dicts (시간 오름차순). 실패 시 빈 list.
    """
    html = fetch_us_calendar_html(timeout=timeout)
    if not html:
        return []
    events = parse_events(html)
    # 시간 필터
    now_kst = datetime.now(timezone(timedelta(hours=9)))
    cutoff_lo = now_kst - timedelta(hours=lookback_hours)
    cutoff_hi = now_kst + timedelta(hours=2)  # 오늘 미국 야간 발표 직전까지

    def _within(e):
        s = e.get("kst")
        if not s:
            return False
        try:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone(timedelta(hours=9)))
        except Exception:
            return False
        return cutoff_lo <= dt <= cutoff_hi

    filtered = [e for e in events if _within(e)]

    # importance 필터
    order = {"high": 3, "medium": 2, "low": 1, None: 0}
    if min_importance:
        threshold = order.get(min_importance.lower(), 2)
        filtered = [e for e in filtered if order.get(e.get("importance"), 0) >= threshold]

    # 시간순 정렬
    filtered.sort(key=lambda e: e.get("kst") or "")
    return filtered


def get_today_us_calendar(timeout: int = 15) -> list[dict]:
    """미국 ET 오늘 발표 예정 이벤트 (사전 안내용)."""
    html = fetch_us_calendar_html(timeout=timeout)
    if not html:
        return []
    events = parse_events(html)
    # ET 기준 오늘
    today_et = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")
    return [e for e in events if e.get("date") == today_et]


if __name__ == "__main__":
    import json
    print("=== Recent US overnight events (last 30h, medium+) ===")
    for e in get_us_overnight_events():
        print(f"  [{e['importance']:>6}] {e['kst']}  {e['title']:<40} actual={e['actual']!s:<10} forecast={e['forecast']!s:<10} previous={e['previous']!s:<10} surprise={e['surprise']!s}")
    print()
    print("=== Today (ET) US calendar — upcoming ===")
    for e in get_today_us_calendar():
        print(f"  [{e.get('importance') or '-':>6}] {e['kst']}  {e['title']:<40} forecast={e['forecast']!s:<10} previous={e['previous']!s:<10}")
