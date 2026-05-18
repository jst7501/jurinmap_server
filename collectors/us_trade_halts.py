"""미국 시장 종목별 거래정지(Trade Halt) 모니터링 — NASDAQ Trader RSS feed.

NASDAQ 이 NYSE/AMEX 까지 통합 공시. 5분 LULD (Limit Up-Limit Down) 정지가
한국 시장의 "사이드카" 와 같은 의미 — 가격 한도 초과로 5분 자동 정지.

Source: https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts

핵심 reason code (사용자 가치 순):
  LUDP — LULD 가격 한도 초과 → **5분 거래정지 ("상킷/하킷")**
  LUDS — LULD Straddle (재개 직후 재정지)
  T5   — 단일 종목 거래정지 (LULD pause)
  T1   — News Pending (재개 미정, 보통 SEC/회사 발표 대기)
  T2   — News Released (재개 임박)
  T6   — Extraordinary Market Activity
  T8   — ETF halt
  T12  — SEC Trading Suspension (장기 정지 가능성)
  M    — Market Maker Compliance
  MWC1/2/3 — Market-Wide Circuit Breaker (전체 시장 정지, 매우 드뭄)
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from xml.etree import ElementTree as ET

import requests

_FEED_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/125.0",
    "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
}
_NS = {"ndaq": "http://www.nasdaqtrader.com/"}  # NASDAQ custom namespace

# Reason code → 한국어 설명 + 정지 종류
REASON_LABELS = {
    "LUDP": ("LULD 가격한도", "limit_pause", "가격 변동 한도 도달 → 5분 자동 정지"),
    "LUDS": ("LULD 재정지", "limit_pause", "재개 직후 재정지 (straddle)"),
    "T5":   ("단일 종목 정지", "limit_pause", "LULD pause"),
    "T1":   ("뉴스 대기", "news", "중요 발표 대기 — 재개 시각 미정"),
    "T2":   ("뉴스 발표", "news", "뉴스 공개 — 재개 임박"),
    "T3":   ("뉴스 발표후 quotation", "news", "뉴스 후 호가 정지"),
    "T6":   ("이상 거래", "volatility", "비정상 거래량·변동 감지"),
    "T8":   ("ETF 정지", "etf", "ETF 가격·NAV 괴리"),
    "T12":  ("SEC 정지", "sec", "SEC Trading Suspension — 장기 가능"),
    "M":    ("마켓 메이커 컴플라이언스", "compliance", "MM 의무 미준수"),
    "H10":  ("SEC 일시 정지", "sec", "SEC 임시 거래 정지"),
    "MWC1": ("시장 전체 정지 Lv1", "marketwide", "S&P500 -7% → 15분 정지"),
    "MWC2": ("시장 전체 정지 Lv2", "marketwide", "S&P500 -13% → 15분 정지"),
    "MWC3": ("시장 전체 정지 Lv3", "marketwide", "S&P500 -20% → 잔여 거래일 정지"),
}

# LULD 발동 시 자동 재개 시간 (5분)
LULD_PAUSE_SECONDS = 300


def _parse_dt(date_str: str, time_str: str) -> Optional[datetime]:
    """05/13/2026 + 19:50:00.000 (ET) → UTC aware datetime."""
    if not date_str or not time_str:
        return None
    try:
        # date: MM/DD/YYYY
        m, d, y = (int(x) for x in date_str.strip().split("/"))
        # time: HH:MM:SS.ms (ET — EDT/EST naive)
        time_str = time_str.strip().split(".")[0]  # drop ms
        hh, mm, ss = (int(x) for x in time_str.split(":"))
        # EDT (4~10월) vs EST (11~3월) 휴리스틱
        et_offset = -4 if 3 <= m <= 10 else -5
        return datetime(y, m, d, hh, mm, ss, tzinfo=timezone(timedelta(hours=et_offset)))
    except Exception:
        return None


_FEED_CACHE: dict = {"xml": None, "ts": 0.0}
_FEED_TTL = 12  # 초 — 같은 halt stats 요청 내 두 번 호출 시 HTTP 1회만


def fetch_feed(timeout: int = 15) -> Optional[str]:
    """RSS feed fetch — 12초 메모리 캐시. utf-8-sig BOM 자동 제거."""
    now = time.monotonic()
    if _FEED_CACHE["xml"] and now - _FEED_CACHE["ts"] < _FEED_TTL:
        return _FEED_CACHE["xml"]
    try:
        r = requests.get(_FEED_URL, headers=_HEADERS, timeout=timeout)
        if r.status_code != 200:
            return None
        text = r.content.decode("utf-8-sig", errors="replace")
        if "<item>" not in text:
            return None
        _FEED_CACHE["xml"] = text
        _FEED_CACHE["ts"] = now
        return text
    except Exception:
        return None


def parse(xml_text: str) -> list[dict]:
    """RSS items → halt event dicts. 시간 ET·UTC·KST 모두 반환."""
    if not xml_text:
        return []
    # BOM 제거 (UTF-8 BOM 으로 시작하는 응답 처리)
    xml_text = xml_text.lstrip("﻿").lstrip()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"[us_trade_halts] XML parse error: {e}")
        return []
    items = root.findall(".//item")
    out: list[dict] = []
    for it in items:
        def t(tag: str) -> str:
            el = it.find(f"ndaq:{tag}", _NS)
            return (el.text or "").strip() if el is not None and el.text else ""

        halt_date = t("HaltDate")
        halt_time = t("HaltTime")
        sym = t("IssueSymbol")
        name = t("IssueName")
        market = t("Market")
        reason = t("ReasonCode")
        pause_threshold = t("PauseThresholdPrice")
        resumption_date = t("ResumptionDate")
        resumption_quote = t("ResumptionQuoteTime")
        resumption_trade = t("ResumptionTradeTime")

        if not sym:
            continue
        et_dt = _parse_dt(halt_date, halt_time)
        utc_dt = et_dt.astimezone(timezone.utc) if et_dt else None
        kst_dt = et_dt.astimezone(timezone(timedelta(hours=9))) if et_dt else None

        # 재개 시각 — LULD 면 +300초, 명시된 경우 그것 우선
        resumed_at_et = None
        if resumption_date and resumption_trade:
            resumed_at_et = _parse_dt(resumption_date, resumption_trade)
        elif et_dt and reason in ("LUDP", "LUDS", "T5"):
            resumed_at_et = et_dt + timedelta(seconds=LULD_PAUSE_SECONDS)

        label_info = REASON_LABELS.get(reason, ("기타 정지", "other", ""))

        out.append({
            "symbol": sym,
            "name": name,
            "market": market,
            "reason_code": reason,
            "reason_kr": label_info[0],
            "halt_type": label_info[1],
            "reason_detail": label_info[2],
            "pause_threshold_price": pause_threshold or None,
            "halt_date": halt_date,
            "halt_time_et": halt_time,
            "halt_at_utc": utc_dt.isoformat(timespec="seconds") if utc_dt else None,
            "halt_at_kst": kst_dt.strftime("%Y-%m-%d %H:%M:%S") if kst_dt else None,
            "resumption_date": resumption_date or None,
            "resumption_quote_time": resumption_quote or None,
            "resumption_trade_time": resumption_trade or None,
            "expected_resume_at_utc": resumed_at_et.astimezone(timezone.utc).isoformat(timespec="seconds") if resumed_at_et else None,
        })
    return out


def get_recent_halts(active_only: bool = False, hours: int = 24, max_items: int = 100) -> list[dict]:
    """최근 N시간 내 halt 이벤트. active_only=True 면 아직 재개 안 된 것만.

    LULD pause(5분)는 발동 후 5분 이내라면 active.
    """
    xml = fetch_feed()
    if not xml:
        return []
    rows = parse(xml)
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=hours)

    filtered = []
    for r in rows:
        try:
            halt_utc = datetime.fromisoformat(r["halt_at_utc"]) if r["halt_at_utc"] else None
        except Exception:
            halt_utc = None
        if not halt_utc or halt_utc < cutoff:
            continue
        if active_only:
            # 재개 시각 없거나 미래면 active
            try:
                resume = datetime.fromisoformat(r["expected_resume_at_utc"]) if r["expected_resume_at_utc"] else None
            except Exception:
                resume = None
            is_active = resume is None or resume > now_utc
            if not is_active:
                continue
        filtered.append(r)
    filtered.sort(key=lambda r: r["halt_at_utc"] or "", reverse=True)
    return filtered[:max_items]


if __name__ == "__main__":
    halts = get_recent_halts(active_only=False, hours=48, max_items=30)
    print(f"=== Recent US trade halts ({len(halts)}) ===")
    print(f"{'sym':<6} {'reason':<6} {'type':<14} {'halt KST':<20} {'resume UTC':<22} {'name'}")
    print("-" * 110)
    for h in halts[:20]:
        print(f"{h['symbol']:<6} {h['reason_code']:<6} {h['halt_type']:<14} {h['halt_at_kst'] or '-':<20} {h['expected_resume_at_utc'] or '-':<22} {h['name'][:50]}")
    # active LULD pauses (5분 정지 활성)
    print()
    active = get_recent_halts(active_only=True, hours=1)
    print(f"=== Active halts (in progress) — {len(active)} ===")
    for h in active:
        print(f"  {h['symbol']:<6} {h['reason_code']:<6} {h['reason_kr']:<20} halt={h['halt_at_kst']} resume={h['expected_resume_at_utc']}")
