"""NASDAQ Threshold Securities 데이터 — Playwright 기반 Incapsula 우회 스크래퍼.

NASDAQ Reg SHO Threshold 데이터는 Incapsula 봇 필터로 직접 download 차단.
브라우저로 실제 페이지를 로드 + JS 렌더 + table 추출 방식으로 우회.

NASDAQ Trader Threshold page:
  https://www.nasdaqtrader.com/Trader.aspx?id=RegSHOThreshold

페이지가 .xls 파일을 download 링크로 제공. JS-rendered table 도 같이 있어서 그것을 파싱.

핵심:
  - chromium headless + stealth UA + viewport 1280x800
  - DOMContentLoaded 후 추가 wait (Incapsula JS 챌린지 통과 시간)
  - 테이블 selector 안정성 위해 여러 fallback

Run: python collectors/us_threshold_nasdaq_playwright.py [YYYYMMDD]
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("collectors.us_threshold_nasdaq_playwright")

_PAGE_URL = "https://www.nasdaqtrader.com/Trader.aspx?id=RegSHOThreshold"
_DOWNLOAD_BASE = "http://www.nasdaqtrader.com/dynamic/symdir/regsho/nasdaqth{date}.txt"


def _format_date(d: date) -> str:
    """NASDAQ Threshold 파일 명에 들어가는 YYYYMMDD 형식."""
    return d.strftime("%Y%m%d")


def _bm_date_iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def fetch_via_playwright(date_yyyymmdd: Optional[str] = None, timeout_ms: int = 30000) -> dict:
    """Playwright 로 NASDAQ Threshold 데이터 수집.

    Args:
      date_yyyymmdd: 예 "20260513". None 이면 어제(주말 자동 보정).
      timeout_ms: 페이지 로드 + JS 챌린지 통과 최대 대기 (default 30s)

    Returns:
      {
        "as_of_date": "2026-05-13",
        "rows": [
          {"symbol":"XXX","name":"...","market":"nasdaq_gs","market_category":"..."}
        ],
        "row_count": N,
        "source": "nasdaq_trader_playwright"
      }
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("playwright not installed")

    if not date_yyyymmdd:
        d = date.today() - timedelta(days=1)
        # 주말 보정 — 금요일까지
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        date_yyyymmdd = _format_date(d)

    txt_url = _DOWNLOAD_BASE.format(date=date_yyyymmdd)

    with sync_playwright() as pw:
        # chromium headless + stealth UA
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        # webdriver flag 숨김
        context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
            """
        )

        page = context.new_page()
        try:
            # 1단계: 메인 페이지 방문해서 Incapsula cookie 받기 (networkidle 까지 대기)
            try:
                page.goto(_PAGE_URL, timeout=timeout_ms, wait_until="networkidle")
                page.wait_for_timeout(1500)  # JS 챌린지 추가 안정화
            except Exception as exc:
                logger.warning("main page load: %s", exc)

            # 1.5단계: 페이지가 가리키는 최신 .txt 링크 파싱 (예: nasdaqth20260512.txt)
            #   요청된 date 가 없으면 page 의 자동 latest 사용
            try:
                html = page.content()
                import re
                m = re.search(r'(http://www\.nasdaqtrader\.com/dynamic/symdir/regsho/nasdaqth\d{8}\.txt)', html)
                if m:
                    txt_url = m.group(1)
            except Exception:
                pass

            # 2단계: .txt 파일 download (cookie 자동 따라감)
            response = page.goto(txt_url, timeout=timeout_ms, wait_until="domcontentloaded")
            if response is None or response.status != 200:
                status = response.status if response else "no_response"
                raise RuntimeError(f"NASDAQ threshold download failed (status={status})")

            # 3단계: 컨텐츠 추출
            content = response.text()
            if not content or "Page Not Available" in content[:500] or "<html" in content[:200].lower():
                # 봇 차단 페이지 반환된 경우
                raise RuntimeError(f"NASDAQ threshold blocked or empty (preview: {content[:200]!r})")

        finally:
            context.close()
            browser.close()

    # 실제 다운로드 URL 에서 날짜 추출 (page 가 최신 자동 선택했으면 그쪽)
    import re
    m_date = re.search(r"nasdaqth(\d{8})\.txt", txt_url)
    if m_date:
        date_yyyymmdd = m_date.group(1)

    # 4단계: pipe-delimited 또는 tab-delimited 텍스트 파싱
    return _parse_nasdaq_threshold_text(content, date_yyyymmdd)


def _parse_nasdaq_threshold_text(content: str, date_yyyymmdd: str) -> dict:
    """NASDAQ Threshold 텍스트 파일 → row list.

    실제 파일 포맷 (pipe-delimited, 2026-05 확인):
      Symbol|Security Name|Market Category|Reg SHO Threshold Flag|Rule 3210|Filler

    Market Category: S = NASDAQ Capital Market 등, G = ETF, N = NYSE 리스팅 등.
    마지막 줄은 timestamp (예: 20260512230005) 로 종결.
    """
    rows = []
    lines = content.split("\n")
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        # 첫 컬럼이 헤더("Symbol") 이거나 단일 timestamp 면 skip
        if len(parts) < 4:
            continue
        symbol = parts[0].upper()
        if not symbol or symbol == "SYMBOL":
            continue
        # threshold flag = Y 만 등록 종목 (혹시 N 이 섞일 수 있음)
        threshold_flag = parts[3].upper() if len(parts) > 3 else ""
        if threshold_flag and threshold_flag not in ("Y", "YES"):
            continue
        rows.append({
            "symbol": symbol,
            "name": parts[1] if len(parts) > 1 else None,
            "market_category": parts[2] if len(parts) > 2 else None,
            "market": "nasdaq",
            "as_of_date": _bm_date_iso(date_yyyymmdd),
        })

    return {
        "as_of_date": _bm_date_iso(date_yyyymmdd),
        "rows": rows,
        "row_count": len(rows),
        "source": "nasdaq_trader_playwright",
    }


def fetch_latest_available(days_back: int = 10) -> dict:
    """최근 N영업일 중 가용 데이터 1개 찾아서 반환."""
    today = date.today()
    last_err: str = "no_attempt"
    for offset in range(days_back):
        d = today - timedelta(days=offset)
        if d.weekday() >= 5:
            continue  # 주말 skip
        dstr = _format_date(d)
        try:
            res = fetch_via_playwright(dstr)
            if res["row_count"] > 0:
                return res
            last_err = f"{dstr}: empty"
        except Exception as exc:
            last_err = f"{dstr}: {exc}"
            time.sleep(2)  # rate limit
    raise RuntimeError(f"no NASDAQ threshold data in last {days_back} days. last error: {last_err}")


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) > 1:
        res = fetch_via_playwright(sys.argv[1])
    else:
        res = fetch_latest_available()
    print(f"date={res['as_of_date']} rows={res['row_count']} source={res['source']}")
    for r in res["rows"][:10]:
        print(f"  {r['symbol']:8} {r['market_category']:12} {(r['name'] or '')[:50]}")
