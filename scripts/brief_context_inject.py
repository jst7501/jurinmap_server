"""brief skill 이 매번 호출할 컨텍스트 주입 helper.

[배경] 2026-05-22 NVDA "어닝쇼크" 오기 + 2026-05-26 "수요일 28일 PCE·엔비디아" 오기
사고 — Claude Code 가 brief 작성 시 (1) 직전 slot 의 사실 표기를 모르고
(2) 이번 주 매크로/어닝 일정을 추측해서 발생.

이 helper 가 brief skill prompt 에 강제 주입되어 같은 사고 방지.

사용:
    python scripts/brief_context_inject.py [--days-back 7] [--days-ahead 10]
        --slot pre|morning|afternoon|post|evening

또는 import 해서 dict 받기:
    from scripts.brief_context_inject import build_brief_context
    ctx = build_brief_context(slot='pre')
    # → {
    #     "recent_briefs": [...],       # 직전 7일 brief 본문 요약
    #     "upcoming_calendar": {...},   # 다음 10일 매크로·어닝·IPO
    #     "consistency_warnings": [...]  # "NVDA 이미 발표됨" 같은 자동 경고
    #   }
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ────────────────────────────────────────────────────────────────────
# 자동 경고 규칙 — 이미 발표된 사건은 미래 시제로 다시 언급 금지

# 키워드 → 과거형 동사 매핑
_PAST_TENSE_MARKERS = (
    "발표됐", "발표 됐", "발표돼", "발표 돼",
    "공개됐", "공개 됐", "공개돼",
    "빅비트", "쇼크", "어닝쇼크", "어닝 쇼크", "어닝 서프라이즈", "어닝서프라이즈",
    "상회", "하회", "부합", "예상치 부합", "컨센서스 부합",
    "기록했어요", "찍었어요", "올랐어요", "내려갔어요", "기록함",
)

# brief 가 자주 혼동하는 매크로/실적 키워드
_EVENT_KEYWORDS = (
    "엔비디아", "NVDA", "엔비", "PCE", "CPI", "PPI", "FOMC", "FED", "옐런", "파월",
    "삼성전자", "SK하이닉스", "TSMC", "어플라이드", "AMAT", "AMD", "MU",
    "애플", "AAPL", "AVGO", "브로드컴",
)


# ────────────────────────────────────────────────────────────────────
def _fetch_recent_briefs(days_back: int = 7) -> list[dict]:
    """직전 N일간 brief 본문 (briefing_date DESC + slot_time DESC)."""
    from server.db.connections import get_stocks_conn

    conn = get_stocks_conn()
    try:
        cur = conn.execute(
            "SELECT briefing_date, slot, slot_time, summary "
            "FROM market_briefings "
            "WHERE briefing_date >= ? "
            "ORDER BY briefing_date DESC, slot_time DESC "
            "LIMIT 25",
            ((datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d"),),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for r in rows:
        d = dict(r) if hasattr(r, "keys") else {
            "briefing_date": r[0], "slot": r[1], "slot_time": r[2], "summary": r[3],
        }
        out.append(d)
    return out


def _detect_consistency_warnings(recent_briefs: list[dict]) -> list[str]:
    """이미 발표된 사건 식별 → "이 키워드는 이미 X일에 발표됐어요" 경고 리스트.

    Claude Code 가 brief 작성 시 prompt 의 이 경고 보고 미래 시제 재언급 차단.
    """
    warnings: list[str] = []
    # 키워드별 가장 최근 과거형 언급 검색
    for kw in _EVENT_KEYWORDS:
        latest_past = None
        for b in recent_briefs:  # DESC 정렬돼 있음
            s = b.get("summary") or ""
            if kw not in s:
                continue
            # 키워드 위치 주변 100자 안에 과거형 마커 있나
            idx = s.find(kw)
            window = s[max(0, idx - 30) : idx + 100]
            if any(m in window for m in _PAST_TENSE_MARKERS):
                latest_past = b
                break
        if latest_past:
            warnings.append(
                f"⚠️  '{kw}' 사건은 이미 {latest_past['briefing_date']} {latest_past['slot']} brief 에서 "
                f"과거형으로 다뤘음 — '~예정' / '~할 거예요' 같은 미래 시제 재언급 금지. "
                f"필요시 '지난 {latest_past['briefing_date']} 발표된 ~' 같이 과거형으로 회상만."
            )
    return warnings


def _fetch_upcoming_calendar(days_ahead: int = 10) -> dict:
    """Finnhub forward calendar — 매크로·어닝·IPO."""
    try:
        from collectors.us_finnhub import get_upcoming_calendar
        return get_upcoming_calendar(days=days_ahead)
    except Exception as exc:
        return {"error": str(exc), "economic": [], "earnings": [], "ipo": []}


# ────────────────────────────────────────────────────────────────────
# 초대형 이벤트 자동 판별 — "스페이스X IPO 같은 거 자동 잡아냄"

# IPO 키워드 watchlist — 사적 회사 중 시장 흡수력 큰 거대 IPO
_MEGA_IPO_KEYWORDS = (
    "spacex", "space x", "스페이스x", "스페이스 x",
    "openai", "open ai", "오픈ai", "오픈 ai",
    "stripe", "스트라이프",
    "databricks", "데이터브릭스",
    "shein", "쉬인",
    "bytedance", "tiktok", "바이트댄스", "틱톡",
    "anthropic", "앤트로픽",
    "xai", "x.ai",
    "klarna", "클라나",
    "discord", "디스코드",
    "epic games", "에픽게임즈",
)

# 매크로 mega 키워드 — 시장 전반 흔드는 지표
_MEGA_MACRO_KEYWORDS = (
    "fomc", "fed funds", "interest rate decision",  # 금리·FOMC
    "nonfarm payrolls", "non-farm",                  # 미국 NFP
    "core pce", "pce price",                         # 핵심 PCE
    "core cpi", "cpi y", "inflation rate y",         # 핵심 CPI
    "gdp growth", "gdp q",                            # GDP
    "unemployment rate",                              # 실업률
)

# 한국 시장 큰 영향 빅테크 어닝 — 자동 mega 판정
_MEGA_EARNINGS_SYMBOLS = (
    "NVDA", "TSM", "AAPL", "MSFT", "AVGO", "AMAT", "ASML", "MU",
)


def _classify_event_size(event_type: str, event: dict) -> str:
    """이벤트 중요도 분류 → 'mega' / 'high' / 'medium' / 'low'.

    Args:
        event_type: 'ipo' / 'earnings' / 'economic'
        event: 해당 이벤트 dict (Finnhub 응답)
    """
    if event_type == "ipo":
        # 거대 IPO 자동 판정 — 시총 또는 키워드
        total_value = event.get("totalSharesValue") or 0
        if isinstance(total_value, (int, float)) and total_value >= 10_000_000_000:  # $10B+
            return "mega"
        if total_value >= 5_000_000_000:  # $5B+
            return "high"
        # 키워드 매칭 — 스페이스X·OpenAI 같은 거대 사적 회사
        name = ((event.get("name") or "") + " " + (event.get("symbol") or "")).lower()
        if any(kw in name for kw in _MEGA_IPO_KEYWORDS):
            return "mega"
        if total_value >= 1_000_000_000:  # $1B+
            return "medium"
        return "low"

    if event_type == "earnings":
        sym = (event.get("symbol") or "").upper()
        if sym in _MEGA_EARNINGS_SYMBOLS:
            return "mega"
        return "medium"

    if event_type == "economic":
        imp = (event.get("importance") or "").lower()
        if imp != "high":
            return "low" if imp == "low" else "medium"
        title = (event.get("event") or "").lower()
        if any(kw in title for kw in _MEGA_MACRO_KEYWORDS):
            return "mega"
        return "high"

    return "low"


def _extract_mega_events(calendar: dict) -> list[dict]:
    """forward calendar 에서 mega/high 이벤트만 추출 + 정규화.

    Returns:
        [{"category": "ipo|earnings|economic", "size": "mega|high",
          "date": "...", "title": "...", "detail": {...원본...}}]
    """
    mega = []
    for ipo in (calendar.get("ipo") or []):
        size = _classify_event_size("ipo", ipo)
        if size in ("mega", "high"):
            mega.append({
                "category": "ipo",
                "size": size,
                "date": ipo.get("date"),
                "title": ipo.get("name") or ipo.get("symbol"),
                "symbol": ipo.get("symbol"),
                "detail": ipo,
            })
    for er in (calendar.get("earnings") or []):
        size = _classify_event_size("earnings", er)
        if size in ("mega", "high"):
            mega.append({
                "category": "earnings",
                "size": size,
                "date": er.get("date"),
                "title": f"{er.get('symbol')} Q{er.get('quarter')} 어닝",
                "symbol": er.get("symbol"),
                "detail": er,
            })
    for ec in (calendar.get("economic") or []):
        size = _classify_event_size("economic", ec)
        if size in ("mega", "high"):
            mega.append({
                "category": "economic",
                "size": size,
                "date": ec.get("date"),
                "title": ec.get("event"),
                "country": ec.get("country"),
                "detail": ec,
            })
    # 정렬: mega 먼저, 그 다음 high. 같은 size 안에선 날짜 빠른 순.
    size_rank = {"mega": 0, "high": 1}
    mega.sort(key=lambda x: (size_rank.get(x["size"], 9), x.get("date") or ""))
    return mega


# ────────────────────────────────────────────────────────────────────
# 2026-06-02 — 분석 강화 데이터 (왜 떨어졌나·왜 올랐나 추측에 필요)
#
# 사용자 피드백: "그냥 장 상황 그대로 읽어주는건 아니다. 떨어졌으면 왜 떨어졌을까
# 추측이나 그런게 필요하다."
#
# 이 helper 들이 brief 작성 시 인과 분석에 필요한 모든 데이터를 inject.
# - today_market_snapshot: 코스피·코스닥·외인 시신선 수급 + 강세/약세 테마 + 환율
# - overnight_us_market: 어제 미국 S&P·NASDAQ·빅테크 (디커플링 분석용)
# - today_top_movers: 등락 상위 종목 (외인 매도 주체 파악)
# - today_macro_results: 오늘 발표된 매크로 (PPI·CPI·FOMC 등)

def _safe_db_query(sql: str, params: tuple = ()) -> list[dict]:
    """안전 DB 조회. 실패 시 빈 리스트."""
    try:
        from server.db.connections import get_stocks_conn
        conn = get_stocks_conn()
        try:
            cur = conn.execute(sql, params) if params else conn.execute(sql)
            rows = cur.fetchall()
            return [dict(r) if hasattr(r, "keys") else dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


def _fetch_today_market_snapshot() -> dict:
    """오늘 KIS 신선 데이터 — 분석의 1차 source."""
    snapshot: dict = {}

    # ① 가장 신선 4주체 수급 (5분 폴링 누적)
    rows = _safe_db_query(
        "SELECT market, fetched_at, foreign_net_uk, institution_net_uk, "
        "individual_net_uk, etc_net_uk, index_price, index_change_pct "
        "FROM market_flow_intraday "
        "WHERE fetched_at >= ? "
        "ORDER BY fetched_at DESC LIMIT 4",
        ((datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),),
    )
    if rows:
        # KOSPI/KOSDAQ row 둘 다 같은 값 (이미 합산) → 첫 row 만
        latest = rows[0]
        snapshot["flow_live"] = {
            "fetched_at": str(latest.get("fetched_at") or "")[:19],
            "unit": "억원",
            "foreign_net_uk": latest.get("foreign_net_uk"),
            "institution_net_uk": latest.get("institution_net_uk"),
            "individual_net_uk": latest.get("individual_net_uk"),
            "etc_net_uk": latest.get("etc_net_uk"),
            "note": "KIS FHPTJ04040000 5분 폴링 누적 — brief.flow_today 보다 신선",
        }

    # ② 코스피·코스닥 현재가 + 변동률
    idx = _safe_db_query(
        "SELECT market, current_price, change_pct, change_amt, volume, fetched_at "
        "FROM indices_intraday WHERE fetched_at >= ? "
        "ORDER BY fetched_at DESC LIMIT 4",
        ((datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),),
    )
    if idx:
        snapshot["indices"] = {}
        for r in idx[:4]:
            mkt = r.get("market")
            if mkt and mkt not in snapshot["indices"]:
                snapshot["indices"][mkt] = {
                    "current": r.get("current_price"),
                    "change_pct": r.get("change_pct"),
                    "change_amt": r.get("change_amt"),
                    "volume": r.get("volume"),
                    "fetched_at": str(r.get("fetched_at") or "")[:19],
                }

    # ③ 주도 테마 (강세 5 + 약세 5) — "왜 떨어졌나" 인과 분석의 핵심
    themes_up = _safe_db_query(
        "SELECT theme, ROUND(AVG(change_pct)::numeric, 2) AS avg_pct, COUNT(*) AS cnt "
        "FROM stock_themes st JOIN price_today pt USING (code) "
        "WHERE pt.change_pct IS NOT NULL "
        "GROUP BY theme HAVING COUNT(*) >= 3 "
        "ORDER BY avg_pct DESC LIMIT 5"
    )
    themes_dn = _safe_db_query(
        "SELECT theme, ROUND(AVG(change_pct)::numeric, 2) AS avg_pct, COUNT(*) AS cnt "
        "FROM stock_themes st JOIN price_today pt USING (code) "
        "WHERE pt.change_pct IS NOT NULL "
        "GROUP BY theme HAVING COUNT(*) >= 3 "
        "ORDER BY avg_pct ASC LIMIT 5"
    )
    snapshot["themes"] = {
        "rising_top5": [{"theme": r.get("theme"), "avg_pct": float(r.get("avg_pct") or 0), "cnt": r.get("cnt")} for r in themes_up],
        "falling_top5": [{"theme": r.get("theme"), "avg_pct": float(r.get("avg_pct") or 0), "cnt": r.get("cnt")} for r in themes_dn],
    }

    # ④ 등락 상위 종목 (외인 매도 주체 파악용)
    gainers = _safe_db_query(
        "SELECT code, name, current_price, change_pct, volume, market_cap "
        "FROM price_today WHERE change_pct IS NOT NULL "
        "ORDER BY change_pct DESC LIMIT 5"
    )
    losers = _safe_db_query(
        "SELECT code, name, current_price, change_pct, volume, market_cap "
        "FROM price_today WHERE change_pct IS NOT NULL "
        "ORDER BY change_pct ASC LIMIT 5"
    )
    snapshot["top_movers"] = {
        "gainers_top5": [{"code": r.get("code"), "name": r.get("name"), "change_pct": float(r.get("change_pct") or 0)} for r in gainers],
        "losers_top5": [{"code": r.get("code"), "name": r.get("name"), "change_pct": float(r.get("change_pct") or 0)} for r in losers],
    }

    return snapshot


def _fetch_overnight_us_market() -> dict:
    """어제 미국 시장 — 디커플링 분석 (미국 ↑ + 한국 ↓ 이유 추측)."""
    snapshot: dict = {}
    try:
        from collectors.macro_collector import MacroCollector
        mc = MacroCollector()
        # S&P/NASDAQ/DowJones/VIX 어제 마감
        macros = mc.get_all_macros() if hasattr(mc, "get_all_macros") else {}
        snapshot["us_indices"] = {
            "sp500": macros.get("sp500") or macros.get("SPX"),
            "nasdaq": macros.get("nasdaq") or macros.get("NDX"),
            "dow": macros.get("dow"),
            "vix": macros.get("vix"),
        }
        # overnight events (어제 KST 밤 ~ 오늘 새벽 발표 매크로)
        if hasattr(mc, "get_us_overnight_events"):
            snapshot["overnight_events"] = mc.get_us_overnight_events(min_importance="medium") or []
    except Exception as exc:
        snapshot["error"] = str(exc)

    # 빅테크 어제 종가 — NVDA·AAPL·MSFT·AVGO·TSM (한국 반도체 영향)
    big_rows = _safe_db_query(
        "SELECT symbol, last_close, last_change_pct, last_close_date "
        "FROM us_stocks WHERE symbol IN ('NVDA','AAPL','MSFT','AVGO','TSM','GOOG','META','AMD','MU','ASML') "
        "AND last_close IS NOT NULL "
        "ORDER BY symbol"
    )
    if big_rows:
        snapshot["us_bigtech"] = [
            {"symbol": r.get("symbol"), "close": r.get("last_close"), "change_pct": r.get("last_change_pct"), "date": str(r.get("last_close_date") or "")[:10]}
            for r in big_rows
        ]

    return snapshot


def _fetch_today_fx_and_macros() -> dict:
    """환율·유가·BTC + 오늘 발표된 매크로."""
    out: dict = {}
    try:
        from collectors.macro_collector import MacroCollector
        mc = MacroCollector()
        macros = mc.get_all_macros() if hasattr(mc, "get_all_macros") else {}
        out["fx"] = {
            "usd_krw": macros.get("usd_krw") or macros.get("USDKRW"),
            "eur_krw": macros.get("eur_krw"),
            "jpy_krw": macros.get("jpy_krw"),
        }
        out["commodities"] = {
            "wti": macros.get("wti"),
            "brent": macros.get("brent"),
            "gold": macros.get("gold"),
            "btc": macros.get("btc") or macros.get("BTC"),
        }
    except Exception as exc:
        out["error"] = str(exc)
    return out


def _fetch_recent_disclosures_top() -> list[dict]:
    """오늘 발표된 임팩트 큰 공시 (DART) — '갑자기 떨어진 이유' 후보."""
    today_str = datetime.now().strftime("%Y%m%d")
    rows = _safe_db_query(
        "SELECT rcept_no, code, name, title, title_kor, summary_kor, impact, rcept_date "
        "FROM dart_disclosures WHERE rcept_date = ? AND impact IN ('down','risk') "
        "ORDER BY id DESC LIMIT 5",
        (today_str,),
    )
    return [
        {
            "code": r.get("code"),
            "name": r.get("name"),
            "title": r.get("title_kor") or r.get("title"),
            "summary": r.get("summary_kor"),
            "impact": r.get("impact"),
        }
        for r in rows
    ]


def build_brief_context(
    slot: str | None = None,
    days_back: int = 7,
    days_ahead: int = 10,
) -> dict:
    """brief skill 이 호출하는 메인 진입점.

    Returns:
        {
          "recent_briefs": [...],            # 직전 7일 brief 본문
          "upcoming_calendar": {...},        # 다음 10일 매크로·어닝·IPO
          "mega_events": [...],              # 자동 + manual 빅 이벤트
          "consistency_warnings": [...],     # "NVDA 이미 발표됨" 자동 경고

          # 2026-06-02 — 분석 강화 (인과 추측에 필요)
          "today_snapshot": {                # 한국 시장 신선 데이터
            "flow_live": {...},              # KIS 5분 폴링 누적 수급
            "indices": {...},                # 코스피·코스닥 현재가
            "themes": {                      # 강세/약세 테마 — 인과 핵심
              "rising_top5": [...],
              "falling_top5": [...],
            },
            "top_movers": {...},             # 등락 종목 5
          },
          "overnight_us": {                  # 미국 어제 — 디커플링 분석
            "us_indices": {...},
            "overnight_events": [...],
            "us_bigtech": [...],
          },
          "fx_macros": {...},                # 환율·유가·BTC
          "today_disclosures_down": [...],   # 오늘 악재 공시
          "fetched_at": "..."
        }
    """
    recent = _fetch_recent_briefs(days_back=days_back)
    calendar = _fetch_upcoming_calendar(days_ahead=days_ahead)
    warnings = _detect_consistency_warnings(recent)

    # 2026-06-02 — 분석 강화 데이터 (병렬 fetch 안 함, sequential — 에러 격리)
    today_snapshot = _fetch_today_market_snapshot()
    overnight_us = _fetch_overnight_us_market()
    fx_macros = _fetch_today_fx_and_macros()
    today_disclosures_down = _fetch_recent_disclosures_top()

    # recent_briefs 는 summary 잘라서 prompt 길이 절약 (각 500자)
    recent_compact = []
    for b in recent[:15]:
        s = b.get("summary") or ""
        recent_compact.append({
            "briefing_date": b.get("briefing_date"),
            "slot": b.get("slot"),
            "slot_time": b.get("slot_time"),
            "summary_excerpt": s[:500],
        })

    mega_events = _extract_mega_events(calendar)

    # 2026-05-26: big_events.json (운영자 수동 등록 — 스페이스X 같은 사적 회사
    # IPO·정성 풀이) 도 mega_events 에 합친다. Finnhub 가 모르는 mega 이벤트를
    # brief 가 누락 안 하도록.
    try:
        from server.routes.events import _load_json_events, _days_until
        manual = _load_json_events() or []
        for ev in manual:
            if ev.get("status") == "completed":
                continue
            dm = _days_until(ev.get("event_date") or "")
            # 14일 안 다가오는 mega/high 만 brief 에 박음 (manual 은 event_date 비어있을 수도)
            if dm is not None and dm < -1:
                continue
            if dm is not None and dm > days_ahead + 7:
                continue
            mega_events.insert(0, {
                "category": ev.get("category", "etc"),
                "size": ev.get("size", "high"),
                "date": ev.get("event_date") or "",
                "title": ev.get("title_kr") or ev.get("title") or "",
                "symbol": ev.get("symbol"),
                "country": ev.get("country"),
                "facts_kr": ev.get("facts_kr") or "",
                "impact_kr": ev.get("impact_kr") or "",
                "headline_kr": ev.get("headline_kr") or "",
                "expected_market_cap_usd": ev.get("expected_market_cap_usd"),
                "related_kr_stocks": ev.get("related_kr_stocks") or [],
                "_source": "manual_big_events",
                "_id": ev.get("id"),
            })
    except Exception as exc:
        # manual 합치기 실패해도 자동 mega_events 는 그대로 반환 (안전)
        pass

    return {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "slot": slot,
        "recent_briefs": recent_compact,
        "upcoming_calendar": calendar,
        "mega_events": mega_events,
        "consistency_warnings": warnings,
        # 2026-06-02 — 분석 강화 (인과 추측 데이터)
        "today_snapshot": today_snapshot,
        "overnight_us": overnight_us,
        "fx_macros": fx_macros,
        "today_disclosures_down": today_disclosures_down,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", default=None)
    ap.add_argument("--days-back", type=int, default=7)
    ap.add_argument("--days-ahead", type=int, default=10)
    args = ap.parse_args()

    ctx = build_brief_context(
        slot=args.slot, days_back=args.days_back, days_ahead=args.days_ahead
    )
    try:
        import io as _io
        sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(json.dumps(ctx, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
