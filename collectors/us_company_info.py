"""미국 종목 회사 정보 — yfinance Ticker.info (무거운 endpoint, 정적 데이터 위주).

수집 필드 (모두 일/주 단위 변동):
  company:
    summary       — longBusinessSummary (영문 사업 소개, 200~2000자)
    industry      — yfinance industry (Software, Biotech 등 세부)
    sector_full   — yfinance sector (Technology, Healthcare)
    employees     — fullTimeEmployees
    website
    country / state / city / hq_address
    ceo           — companyOfficers[0].name (가장 상위 임원)

  shares (페니 손바꿈 비율 계산용):
    shares_outstanding — 발행주식수
    float_shares       — 유통주식수
    insider_pct        — 내부자 보유율 %
    institutional_pct  — 기관 보유율 %

  trading (정적):
    avg_volume_10d
    avg_volume_3m
    fifty_two_week_high / low
    beta

  valuation (정적, NULL OK):
    trailing_pe / forward_pe
    price_to_book
    dividend_yield_pct
    market_cap_usd (이미 us_stocks 에 있음)

ThreadPoolExecutor 로 batch fetch. fast_info 보다 무거우니 workers 작게 (5).
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

logger = logging.getLogger("collectors.us_company_info")


def _safe_int(v) -> Optional[int]:
    try:
        if v is None:
            return None
        n = int(v)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _safe_float(v) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _safe_str(v, max_len: int = 200) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s[:max_len] if s else None


def fetch_company_info(symbol: str) -> Optional[dict]:
    """단일 종목 회사 정보 수집. 실패 시 None."""
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info or {}
    except Exception as exc:
        logger.debug("info fetch failed (%s): %s", symbol, exc)
        return None

    if not info or not info.get("symbol"):
        return None

    # 임원 전체 — companyOfficers 배열을 정리해서 JSON 으로 저장
    ceo_name = None
    ceo_title = None
    officers_clean: list[dict] = []
    raw_officers = info.get("companyOfficers") or []
    if raw_officers and isinstance(raw_officers, list):
        for o in raw_officers[:20]:   # 최대 20명
            if not isinstance(o, dict):
                continue
            name = _safe_str(o.get("name"), 100)
            title = _safe_str(o.get("title"), 120)
            if not name:
                continue
            officers_clean.append({
                "name": name,
                "title": title,
                "age": _safe_int(o.get("age")),
                "year_born": _safe_int(o.get("yearBorn")),
                "total_pay": _safe_int(o.get("totalPay")),
                "exercised_value": _safe_int(o.get("exercisedValue")),
                "unexercised_value": _safe_int(o.get("unexercisedValue")),
            })
        if officers_clean:
            ceo_name = officers_clean[0]["name"]
            ceo_title = officers_clean[0]["title"]

    return {
        "symbol": symbol.upper(),
        # 회사 프로필
        "summary": _safe_str(info.get("longBusinessSummary"), 4000),
        "industry": _safe_str(info.get("industry"), 100),
        "sector_full": _safe_str(info.get("sector"), 80),
        "employees": _safe_int(info.get("fullTimeEmployees")),
        "website": _safe_str(info.get("website"), 200),
        "country": _safe_str(info.get("country"), 50),
        "state": _safe_str(info.get("state"), 50),
        "city": _safe_str(info.get("city"), 50),
        "hq_address": _safe_str(info.get("address1"), 200),
        "ceo_name": ceo_name,
        "ceo_title": ceo_title,
        "officers": officers_clean,   # 임원 전체 (최대 20명)
        # 주식 구조
        "shares_outstanding": _safe_int(info.get("sharesOutstanding")),
        "float_shares": _safe_int(info.get("floatShares")),
        "insider_pct": _safe_float(info.get("heldPercentInsiders")),       # 0~1
        "institutional_pct": _safe_float(info.get("heldPercentInstitutions")),
        # 거래량 / 변동성
        "avg_volume_10d": _safe_int(info.get("averageVolume10days") or info.get("averageDailyVolume10Day")),
        "avg_volume_3m": _safe_int(info.get("averageVolume")),
        "fifty_two_week_high": _safe_float(info.get("fiftyTwoWeekHigh")),
        "fifty_two_week_low": _safe_float(info.get("fiftyTwoWeekLow")),
        "beta": _safe_float(info.get("beta")),
        # 밸류에이션
        "trailing_pe": _safe_float(info.get("trailingPE")),
        "forward_pe": _safe_float(info.get("forwardPE")),
        "price_to_book": _safe_float(info.get("priceToBook")),
        "dividend_yield_pct": _safe_float(info.get("dividendYield")),     # 0~1
    }


def batch_fetch_company_info(
    symbols: list[str],
    workers: int = 5,
    progress_callback=None,
) -> dict[str, Optional[dict]]:
    """N 종목 → {sym: info_dict or None}. 실패 종목 None 처리."""
    out: dict[str, Optional[dict]] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_company_info, s): s for s in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                out[sym] = fut.result()
            except Exception:
                out[sym] = None
            done += 1
            if progress_callback and done % 20 == 0:
                progress_callback(done, len(symbols))
    return out


if __name__ == "__main__":
    import sys
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    syms = sys.argv[1:] if len(sys.argv) > 1 else ["ALP", "WOK", "NVDA", "AAPL"]
    res = batch_fetch_company_info(syms, workers=3)
    for sym, info in res.items():
        if not info:
            print(f"{sym}: (정보 없음)")
            continue
        print(f"\n=== {sym} ===")
        print(f"  industry: {info.get('industry')}")
        print(f"  sector: {info.get('sector_full')}")
        print(f"  employees: {info.get('employees')}")
        print(f"  CEO: {info.get('ceo_name')} ({info.get('ceo_title')})")
        print(f"  website: {info.get('website')}")
        print(f"  country/city: {info.get('country')}/{info.get('city')}")
        print(f"  shares_out: {info.get('shares_outstanding')}, float: {info.get('float_shares')}")
        if info.get('float_shares') and info.get('avg_volume_10d'):
            turnover = info['avg_volume_10d'] / info['float_shares']
            print(f"  손바꿈 회전율 (avg vol / float): {turnover*100:.1f}%/일")
        print(f"  summary: {(info.get('summary') or '(없음)')[:120]}...")
