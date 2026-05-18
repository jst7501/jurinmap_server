"""미국 전체 ticker 마스터 — NASDAQ Trader 공식 symbol directory.

두 파일 통합:
  nasdaqlisted.txt   — NASDAQ 상장 (~5400 종목)
  otherlisted.txt    — NYSE / AMEX / ARCA / NYSE National 등 (~7200 종목)

총 ~12,500 종목 + 회사명. 사용처:
  - us_stocks 마스터 보강 (Reddit ticker 매칭, 검색 추천)
  - ALL-CAPS ticker 검증 (sentiment 추출 시 false positive 제거)
  - 거래소 분류 (NASDAQ / NYSE / AMEX / ARCA)

NASDAQ Trader 는 매일 갱신 + 무료 + 공식. 백업 ftp.nasdaqtrader.com 도 동일.
"""
from __future__ import annotations

import logging
from typing import Iterator

import requests

logger = logging.getLogger("collectors.us_ticker_master")

_BASE = "https://www.nasdaqtrader.com/dynamic/SymDir"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JurinMapBot/1.0)"}

# NASDAQ Trader Exchange 코드 → 표준 거래소명
_EXCHANGE_MAP = {
    "A": "NYSE_AMEX",       # American
    "N": "NYSE",
    "P": "NYSE_ARCA",
    "Z": "BATS",
    "V": "IEXG",
}

# Market Category (NASDAQ 내부 분류)
_NASDAQ_CATEGORY = {
    "Q": "NASDAQ_GS",   # Global Select
    "G": "NASDAQ_GM",   # Global Market
    "S": "NASDAQ_CM",   # Capital Market
}


def _fetch_text(url: str) -> str:
    r = requests.get(url, headers=_HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def parse_nasdaq_listed(text: str) -> Iterator[dict]:
    """nasdaqlisted.txt — NASDAQ 상장 ticker.

    Columns: Symbol|Security Name|Market Category|Test Issue|Financial Status|
             Round Lot Size|ETF|NextShares
    """
    lines = text.split("\n")
    if not lines:
        return
    # 헤더 검증
    if "Symbol" not in lines[0]:
        logger.warning("unexpected nasdaqlisted header: %s", lines[0][:100])
        return
    for line in lines[1:]:
        line = line.strip()
        if not line or line.startswith("File Creation"):
            continue
        parts = line.split("|")
        if len(parts) < 7:
            continue
        symbol = parts[0].strip().upper()
        if not symbol or symbol == "SYMBOL":
            continue
        test_issue = parts[3].strip().upper()
        if test_issue == "Y":
            continue   # 테스트 종목 skip
        is_etf = parts[6].strip().upper() == "Y"
        market_cat = parts[2].strip().upper()
        yield {
            "ticker": symbol,
            "name": parts[1].strip(),
            "exchange": "NASDAQ",
            "market_category": _NASDAQ_CATEGORY.get(market_cat, market_cat),
            "is_etf": is_etf,
        }


def parse_other_listed(text: str) -> Iterator[dict]:
    """otherlisted.txt — NYSE/AMEX/ARCA/BATS 등 NASDAQ 외 거래소.

    Columns: ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|
             Test Issue|NASDAQ Symbol
    """
    lines = text.split("\n")
    if not lines:
        return
    if "ACT Symbol" not in lines[0] and "Symbol" not in lines[0]:
        logger.warning("unexpected otherlisted header: %s", lines[0][:100])
        return
    for line in lines[1:]:
        line = line.strip()
        if not line or line.startswith("File Creation"):
            continue
        parts = line.split("|")
        if len(parts) < 7:
            continue
        symbol = parts[0].strip().upper()
        if not symbol or symbol == "ACT SYMBOL":
            continue
        test_issue = parts[6].strip().upper()
        if test_issue == "Y":
            continue
        exchange_code = parts[2].strip().upper()
        is_etf = parts[4].strip().upper() == "Y"
        yield {
            "ticker": symbol,
            "name": parts[1].strip(),
            "exchange": _EXCHANGE_MAP.get(exchange_code, exchange_code or "OTHER"),
            "market_category": None,
            "is_etf": is_etf,
        }


def fetch_all_us_tickers() -> list[dict]:
    """NASDAQ + Other 합쳐서 dedupe ticker list 반환."""
    out: dict[str, dict] = {}

    try:
        nasdaq_text = _fetch_text(f"{_BASE}/nasdaqlisted.txt")
        for row in parse_nasdaq_listed(nasdaq_text):
            out[row["ticker"]] = row
    except Exception as exc:
        logger.error("nasdaqlisted fetch failed: %s", exc)

    try:
        other_text = _fetch_text(f"{_BASE}/otherlisted.txt")
        for row in parse_other_listed(other_text):
            # NASDAQ 에 같은 ticker 있으면 NASDAQ 우선 유지
            if row["ticker"] in out:
                continue
            out[row["ticker"]] = row
    except Exception as exc:
        logger.error("otherlisted fetch failed: %s", exc)

    return list(out.values())


if __name__ == "__main__":
    rows = fetch_all_us_tickers()
    print(f"total tickers: {len(rows)}")
    # exchange 분포
    from collections import Counter
    by_ex = Counter(r["exchange"] for r in rows)
    for ex, n in by_ex.most_common():
        print(f"  {ex}: {n}")
    # sample
    print("\nsample (NASDAQ):")
    for r in rows[:5]:
        print(f"  {r['ticker']:7} | {r['exchange']:12} | {r['name'][:60]}")
    print("\nETF 비율:", sum(1 for r in rows if r["is_etf"]), "/", len(rows))
