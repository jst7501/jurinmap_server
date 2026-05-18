"""미국 종목 공매도 종합 수집 — 다중 소스 통합.

소스 4개:
  1. yfinance.info — sharesShort, sharesShortPriorMonth, shortPercentOfFloat,
                      shortRatio(DTC), dateShortInterest
  2. stockanalysis.com — Short Interest 절대값 + 이전월 + Net Borrowing + Float
                          (스크래핑, robots.txt OK)
  3. iBorrowDesk — borrow_fee_pct, available_shares (이미 us_short_borrow_daily)
  4. FINRA Reg SHO — daily short volume (이미 us_short_volume_daily)

추가 계산:
  short_change_mom_pct — 전월 대비 SI 변화율 (페니 모멘텀 핵심)
  utilization_pct      — (float - available) / float (대여 사용률)
  cost_to_borrow_pct   — borrow_fee_pct
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import requests

logger = logging.getLogger("collectors.us_short_enhanced")

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JurinMapBot/1.0)"}


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
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _parse_num(s: str) -> Optional[float]:
    """stockanalysis.com 의 '1.28M', '68,595', '4.80%', 'n/a' 파싱."""
    if not s or s in ("n/a", "N/A", "-", ""):
        return None
    s = s.replace(",", "").replace("%", "").replace("$", "").strip()
    multiplier = 1
    if s.endswith("T"):
        multiplier = 1e12
        s = s[:-1]
    elif s.endswith("B"):
        multiplier = 1e9
        s = s[:-1]
    elif s.endswith("M"):
        multiplier = 1e6
        s = s[:-1]
    elif s.endswith("K"):
        multiplier = 1e3
        s = s[:-1]
    try:
        return float(s) * multiplier
    except ValueError:
        return None


def fetch_yfinance_short(symbol: str) -> dict:
    """yfinance.info 의 short 관련 필드 직접 추출."""
    try:
        import yfinance as yf
        from datetime import datetime, timezone
        info = yf.Ticker(symbol).info or {}
    except Exception as exc:
        logger.debug("yfinance fetch failed (%s): %s", symbol, exc)
        return {}

    shares_short = _safe_int(info.get("sharesShort"))
    shares_short_prior = _safe_int(info.get("sharesShortPriorMonth"))
    short_pct_float = _safe_float(info.get("shortPercentOfFloat"))   # 0~1
    short_pct_shares = _safe_float(info.get("shortPercentOfSharesOutstanding")) if "shortPercentOfSharesOutstanding" in info else None
    short_ratio = _safe_float(info.get("shortRatio"))   # DTC

    # 전월 대비 변화율
    short_change_mom_pct = None
    if shares_short and shares_short_prior and shares_short_prior > 0:
        short_change_mom_pct = round((shares_short - shares_short_prior) / shares_short_prior * 100, 1)

    # 일자 변환
    def _epoch_to_date(v):
        if not isinstance(v, (int, float)) or v <= 0:
            return None
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return None

    return {
        "shares_short": shares_short,
        "shares_short_prior_month": shares_short_prior,
        "short_change_mom_pct": short_change_mom_pct,
        "short_percent_of_float_pct": (short_pct_float * 100) if short_pct_float else None,
        "short_percent_of_shares_pct": (short_pct_shares * 100) if short_pct_shares else None,
        "days_to_cover": short_ratio,
        "date_short_interest": _epoch_to_date(info.get("dateShortInterest")),
        "date_short_prior": _epoch_to_date(info.get("sharesShortPreviousMonthDate")),
        "source": "yfinance_info",
    }


def fetch_stockanalysis_short(symbol: str) -> dict:
    """stockanalysis.com /statistics/ 페이지 스크래핑 — short interest 통계."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return {}

    url = f"https://stockanalysis.com/stocks/{symbol.lower()}/statistics/"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=15)
        if r.status_code != 200:
            return {}
    except Exception as exc:
        logger.debug("stockanalysis fetch failed (%s): %s", symbol, exc)
        return {}

    soup = BeautifulSoup(r.text, "html.parser")

    targets = {
        "float": "Float",
        "short_interest": "Short Interest",
        "short_interest_prior_month": "Short Previous Month",
        "short_pct_shares_out": "Short % of Shares Out",
        "short_pct_float": "Short % of Float",
        "short_ratio_dtc": "Short Ratio (days to cover)",
        "net_borrowing": "Net Borrowing",
    }

    out: dict = {}
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["th", "td"])]
            if len(cells) < 2:
                continue
            label = cells[0]
            value = cells[1]
            for key, target_label in targets.items():
                if target_label.lower() in label.lower() and key not in out:
                    out[key] = _parse_num(value)
                    break

    if not out:
        return {}
    out["source"] = "stockanalysis"
    return out


def fetch_short_all(symbol: str) -> dict:
    """yfinance + stockanalysis 통합 — 가능한 모든 short 데이터."""
    yf_data = fetch_yfinance_short(symbol)
    sa_data = fetch_stockanalysis_short(symbol)

    # 통합 — stockanalysis 가 더 자세하면 우선
    return {
        "symbol": symbol.upper(),
        # SI 절대값
        "shares_short": yf_data.get("shares_short") or (int(sa_data.get("short_interest")) if sa_data.get("short_interest") else None),
        "shares_short_prior": yf_data.get("shares_short_prior_month") or (int(sa_data.get("short_interest_prior_month")) if sa_data.get("short_interest_prior_month") else None),
        "short_change_mom_pct": yf_data.get("short_change_mom_pct"),
        # 비율
        "short_pct_float": yf_data.get("short_percent_of_float_pct") or sa_data.get("short_pct_float"),
        "short_pct_shares_out": yf_data.get("short_percent_of_shares_pct") or sa_data.get("short_pct_shares_out"),
        # DTC
        "days_to_cover": yf_data.get("days_to_cover") or sa_data.get("short_ratio_dtc"),
        # 일자
        "date_short_interest": yf_data.get("date_short_interest"),
        "date_short_prior": yf_data.get("date_short_prior"),
        # 기타 (stockanalysis 만)
        "float_shares_sa": sa_data.get("float"),
        "net_borrowing": sa_data.get("net_borrowing"),
        # source meta
        "has_yfinance": bool(yf_data),
        "has_stockanalysis": bool(sa_data),
    }


if __name__ == "__main__":
    import sys
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    syms = sys.argv[1:] if len(sys.argv) > 1 else ["ADIL", "ALP", "WOK"]
    for sym in syms:
        d = fetch_short_all(sym)
        print(f"\n=== {sym} ===  sources: yf={d['has_yfinance']} sa={d['has_stockanalysis']}")
        print(f"  Shares Short:       {d.get('shares_short'):,}" if d.get('shares_short') else "  Shares Short:       -")
        print(f"  Prior month:        {d.get('shares_short_prior'):,}" if d.get('shares_short_prior') else "  Prior month:        -")
        if d.get('short_change_mom_pct') is not None:
            print(f"  MoM change:         {d['short_change_mom_pct']:+.1f}%")
        if d.get('short_pct_float') is not None:
            print(f"  % of Float:         {d['short_pct_float']:.2f}%")
        if d.get('days_to_cover') is not None:
            print(f"  Days to Cover:      {d['days_to_cover']:.2f}")
        if d.get('date_short_interest'):
            print(f"  SI date:            {d['date_short_interest']}")
