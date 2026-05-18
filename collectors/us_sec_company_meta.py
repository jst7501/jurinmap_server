"""SEC EDGAR submissions API — 회사 공식 메타 데이터.

URL: https://data.sec.gov/submissions/CIK{cik}.json

수집 가치 (yfinance 보다 정확):
  - sic / sicDescription   — 산업 코드 + 설명 (반도체, 의약품 등 4자리)
  - formerNames            — 이전 회사명 (페니 리브랜딩 추적 핵심!)
  - stateOfIncorporation   — 등기 state (DE 가 대부분)
  - fiscalYearEnd          — 회계연도 종료 (MMDD)
  - category               — Filer 분류 (Large accelerated / Small reporting 등)
  - ein                    — Employer ID
  - phone                  — 회사 대표 전화
  - addresses              — mailing/business 주소 (정확)
  - tickers, exchanges     — 거래소 정보 (다중 ticker 가능)
  - description, website   — yfinance 와 동일

SEC rate limit: 10 req/sec (User-Agent 에 email 필수).
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger("collectors.us_sec_company_meta")

_HEADERS = {
    "User-Agent": "JurinMapBot research@example.com",
    "Accept": "application/json",
    "Host": "data.sec.gov",
}

# CIK 매핑 (us_company_news 와 공유 — 같은 데이터 소스)
_CIK_CACHE: dict[str, str] = {}
_CIK_CACHE_LOADED = False


def _load_cik_mapping() -> None:
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


def fetch_sec_meta(symbol: str) -> Optional[dict]:
    """단일 종목 SEC submissions 메타 데이터."""
    _load_cik_mapping()
    cik = _CIK_CACHE.get(symbol.upper())
    if not cik:
        return None

    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        logger.debug("SEC submissions failed (%s/%s): %s", symbol, cik, exc)
        return None

    # 주소 — business (실제 사업장) 우선, 없으면 mailing
    addresses = data.get("addresses") or {}
    biz = addresses.get("business") or {}
    mail = addresses.get("mailing") or {}
    addr = biz if biz.get("street1") else mail
    address_full = None
    if addr:
        parts = [
            addr.get("street1"), addr.get("street2"),
            addr.get("city"),
            addr.get("stateOrCountry"),
            addr.get("zipCode"),
        ]
        address_full = ", ".join(p for p in parts if p)

    # 이전 회사명 — list of {name, from, to}
    former_names = data.get("formerNames") or []
    former_names_clean = []
    for fn in former_names[:5]:
        former_names_clean.append({
            "name": fn.get("name"),
            "from": (fn.get("from") or "")[:10],   # YYYY-MM-DD
            "to": (fn.get("to") or "")[:10],
        })

    # fiscalYearEnd: "0131" → "01/31"
    fye_raw = data.get("fiscalYearEnd") or ""
    fiscal_year_end = None
    if len(fye_raw) == 4:
        fiscal_year_end = f"{fye_raw[:2]}/{fye_raw[2:]}"

    return {
        "symbol": symbol.upper(),
        "cik": cik,
        "sec_name": (data.get("name") or "").strip() or None,    # SEC 공식 등록명
        "sic_code": (data.get("sic") or "").strip() or None,
        "sic_description": (data.get("sicDescription") or "").strip() or None,
        "exchanges": data.get("exchanges") or [],
        "former_names": former_names_clean,
        "state_of_incorporation": (data.get("stateOfIncorporation") or "").strip() or None,
        "fiscal_year_end": fiscal_year_end,
        "filer_category": (data.get("category") or "").strip() or None,
        "ein": (data.get("ein") or "").strip() or None,
        "phone": (data.get("phone") or "").strip() or None,
        "sec_website": (data.get("website") or "").strip() or None,
        "investor_website": (data.get("investorWebsite") or "").strip() or None,
        "business_address": address_full,
        "_addresses_raw": addresses,
    }


def batch_fetch_sec_meta(symbols: list[str], delay: float = 0.12) -> dict[str, Optional[dict]]:
    """순차 fetch — SEC rate limit 10 req/sec (보수적 0.12s sleep)."""
    out: dict[str, Optional[dict]] = {}
    for sym in symbols:
        out[sym] = fetch_sec_meta(sym)
        time.sleep(delay)
    return out


if __name__ == "__main__":
    import sys
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    syms = sys.argv[1:] if len(sys.argv) > 1 else ["NVDA", "ALP", "WOK", "AAPL"]
    for sym in syms:
        info = fetch_sec_meta(sym)
        if not info:
            print(f"\n{sym}: (CIK 없음 또는 SEC 미등록)")
            continue
        print(f"\n=== {sym} (CIK {info['cik']}) ===")
        print(f"  SEC name: {info['sec_name']}")
        print(f"  SIC: {info['sic_code']} — {info['sic_description']}")
        print(f"  state: {info['state_of_incorporation']}")
        print(f"  fiscal year end: {info['fiscal_year_end']}")
        print(f"  filer category: {info['filer_category']}")
        print(f"  EIN: {info['ein']} · phone: {info['phone']}")
        print(f"  exchanges: {info['exchanges']}")
        print(f"  address: {info['business_address']}")
        if info["former_names"]:
            print(f"  former names ({len(info['former_names'])}개):")
            for fn in info["former_names"]:
                print(f"    - {fn['name']} ({fn['from']} ~ {fn['to'] or 'present'})")
