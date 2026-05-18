"""SEC EDGAR Company Facts API — 분기/연 재무 시계열.

URL: https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json
응답: us-gaap concept 별 분기/연 값 — 600+ concept.

핵심 concept (페니 cash runway / burn rate 분석용):
  Revenues / RevenueFromContractWithCustomerExcludingAssessedTax
  NetIncomeLoss               — 분기 손실 (cash burn proxy)
  Assets / Liabilities / StockholdersEquity
  CashAndCashEquivalentsAtCarryingValue — 현금 잔고
  CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents — 제한현금 포함
  OperatingCashFlow / NetCashProvidedByUsedInOperatingActivities — 영업 현금흐름
  CommonStockSharesOutstanding — 발행주식수 (희석 추적)

cash_runway_months = Cash / |monthly_burn| (영업 적자 시)
"""
from __future__ import annotations

import logging
from typing import Optional

import requests

logger = logging.getLogger("collectors.us_sec_facts")

_HEADERS = {
    "User-Agent": "JurinMapBot research@example.com",
    "Accept": "application/json",
    "Host": "data.sec.gov",
}

# CIK 매핑 (다른 SEC collector 와 공유)
_CIK_CACHE: dict[str, str] = {}
_CIK_CACHE_LOADED = False


def _load_cik_mapping():
    global _CIK_CACHE_LOADED
    if _CIK_CACHE_LOADED:
        return
    try:
        r = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": "JurinMapBot research@example.com"},
            timeout=20,
        )
        r.raise_for_status()
        for v in r.json().values():
            t, cik = v.get("ticker"), v.get("cik_str")
            if t and cik:
                _CIK_CACHE[t.upper()] = str(cik).zfill(10)
        _CIK_CACHE_LOADED = True
    except Exception as exc:
        logger.warning("CIK mapping load failed: %s", exc)


# 추출할 핵심 concept (페니 분석)
_REVENUE_KEYS = [
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
]
_NET_INCOME_KEYS = ["NetIncomeLoss", "ProfitLoss"]
_ASSETS_KEYS = ["Assets"]
_LIAB_KEYS = ["Liabilities", "LiabilitiesAndStockholdersEquity"]
_EQUITY_KEYS = ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]
_CASH_KEYS = [
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    "Cash",
]
_OPCASH_KEYS = [
    "NetCashProvidedByUsedInOperatingActivities",
    "CashFlowFromOperatingActivities",
]
_SHARES_KEYS = [
    "CommonStockSharesOutstanding",
    "EntityCommonStockSharesOutstanding",
]


def _extract_latest(facts_gaap: dict, keys: list[str], unit: str = "USD") -> Optional[dict]:
    """첫 매칭 키의 가장 최근 (end 가장 큰) 값."""
    for k in keys:
        c = facts_gaap.get(k, {})
        units = c.get("units", {})
        u_list = units.get(unit) or units.get("shares") or []
        if not u_list:
            continue
        # form 우선순위: 10-K > 10-Q > 그 외
        def _form_rank(r):
            f = r.get("form", "")
            return 0 if f == "10-K" else 1 if f == "10-Q" else 2
        sorted_list = sorted(u_list, key=lambda r: (r.get("end") or "", -_form_rank(r)))
        if sorted_list:
            return sorted_list[-1]
    return None


def _extract_series(facts_gaap: dict, keys: list[str], unit: str = "USD", quarters_only: bool = False, limit: int = 8) -> list[dict]:
    """첫 매칭 키의 시계열 (분기 또는 연). limit 최근 N개."""
    for k in keys:
        c = facts_gaap.get(k, {})
        units = c.get("units", {})
        u_list = units.get(unit) or units.get("shares") or []
        if not u_list:
            continue
        if quarters_only:
            # 분기는 fp = Q1/Q2/Q3/Q4 또는 form = 10-Q
            u_list = [r for r in u_list if r.get("form") in ("10-Q", "10-K")]
        # end 내림차순
        u_list = sorted(u_list, key=lambda r: r.get("end") or "", reverse=True)
        # dedupe by end (10-K 우선)
        seen_ends = set()
        deduped = []
        for r in u_list:
            end = r.get("end")
            if end in seen_ends:
                continue
            seen_ends.add(end)
            deduped.append(r)
        return deduped[:limit]
    return []


def fetch_sec_facts(symbol: str) -> Optional[dict]:
    _load_cik_mapping()
    cik = _CIK_CACHE.get(symbol.upper())
    if not cik:
        return None

    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        logger.debug("SEC facts failed (%s): %s", symbol, exc)
        return None

    facts = data.get("facts", {})
    gaap = facts.get("us-gaap", {}) or {}

    # 최신 단일 값
    revenue = _extract_latest(gaap, _REVENUE_KEYS)
    net_income = _extract_latest(gaap, _NET_INCOME_KEYS)
    assets = _extract_latest(gaap, _ASSETS_KEYS)
    liabilities = _extract_latest(gaap, _LIAB_KEYS)
    equity = _extract_latest(gaap, _EQUITY_KEYS)
    cash = _extract_latest(gaap, _CASH_KEYS)
    op_cash = _extract_latest(gaap, _OPCASH_KEYS)
    shares = _extract_latest(gaap, _SHARES_KEYS, unit="shares")

    # 분기 시계열 (최근 8 = 2년치)
    revenue_q = _extract_series(gaap, _REVENUE_KEYS, quarters_only=True, limit=8)
    ni_q = _extract_series(gaap, _NET_INCOME_KEYS, quarters_only=True, limit=8)
    cash_series = _extract_series(gaap, _CASH_KEYS, quarters_only=True, limit=8)
    opcash_series = _extract_series(gaap, _OPCASH_KEYS, quarters_only=True, limit=8)

    # Cash runway 추정 — 최근 1년 (4분기) 영업 적자 합산 → 월 burn → cash / burn
    cash_runway_months = None
    burn_monthly = None
    if cash and op_cash and op_cash.get("val") and op_cash["val"] < 0:
        # opcash 연간 (negative) → monthly burn
        # form 10-K (annual) 우선
        last_opcash_annual = None
        for r in (_extract_series(gaap, _OPCASH_KEYS, quarters_only=False, limit=2) or []):
            if r.get("form") == "10-K":
                last_opcash_annual = r
                break
        if last_opcash_annual and last_opcash_annual["val"] < 0:
            monthly = abs(last_opcash_annual["val"]) / 12
            burn_monthly = int(monthly)
            cash_val = cash["val"]
            if cash_val > 0 and monthly > 0:
                cash_runway_months = round(cash_val / monthly, 1)

    return {
        "symbol": symbol.upper(),
        "cik": cik,
        "entity_name": data.get("entityName"),
        "latest": {
            "revenue": revenue,
            "net_income": net_income,
            "assets": assets,
            "liabilities": liabilities,
            "equity": equity,
            "cash": cash,
            "op_cash": op_cash,
            "shares_outstanding": shares,
        },
        "series": {
            "revenue_q": revenue_q,
            "net_income_q": ni_q,
            "cash_q": cash_series,
            "op_cash_q": opcash_series,
        },
        "burn_monthly_usd": burn_monthly,
        "cash_runway_months": cash_runway_months,
    }


if __name__ == "__main__":
    import sys
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    syms = sys.argv[1:] if len(sys.argv) > 1 else ["NVDA", "ALP", "WOK"]
    for sym in syms:
        f = fetch_sec_facts(sym)
        if not f:
            print(f"\n{sym}: (CIK 없음 또는 facts 없음)")
            continue
        l = f["latest"]
        print(f"\n=== {sym} ({f['entity_name']}) ===")

        def _fmt(item):
            if not item:
                return "(없음)"
            v = item.get("val")
            v_s = f"${v/1e9:.2f}B" if abs(v) >= 1e9 else f"${v/1e6:.1f}M" if abs(v) >= 1e6 else f"${v:,.0f}"
            return f"{v_s} (end {item.get('end')}, {item.get('form')})"

        print(f"  Revenue:    {_fmt(l['revenue'])}")
        print(f"  NetIncome:  {_fmt(l['net_income'])}")
        print(f"  Cash:       {_fmt(l['cash'])}")
        print(f"  OpCash:     {_fmt(l['op_cash'])}")
        print(f"  Assets:     {_fmt(l['assets'])}")
        print(f"  Equity:     {_fmt(l['equity'])}")
        if f.get("burn_monthly_usd"):
            print(f"  ↘ 월 burn: ${f['burn_monthly_usd']/1e6:.1f}M, runway: {f['cash_runway_months']}개월")
