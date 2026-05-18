"""SEC EDGAR Submissions API — recent filings feed.

URL: https://data.sec.gov/submissions/CIK{cik}.json
응답 핵심:
  filings.recent.form           - array (8-K, 10-K, S-1, 424B5, S-3, F-1, ...)
  filings.recent.filingDate     - array (YYYY-MM-DD)
  filings.recent.accessionNumber- array (0001234567-25-000001)
  filings.recent.primaryDocument
  filings.recent.primaryDocDescription
  filings.recent.items          - array (8-K Item 1.01 등)
  filings.recent.reportDate     - array
  filings.recent.size           - array (bytes)
  filings.recent.isXBRL         - array

페니 단타에서 추적해야 할 form:
  8-K     - 임시 공시 (계약·인사·재무 변동) → AI 요약 대상
  S-1     - IPO 등록 (신규 상장 또는 dilution)
  S-3     - 일반 등록 (간소화된 secondary offering)
  424B5   - prospectus supplement (실제 offering 발표!)
  424B4   - prospectus (IPO 직전)
  F-1     - 외국 발행자 IPO
  F-3     - 외국 발행자 secondary offering
  6-K     - 외국 발행자 임시 공시 (페니 ADR 다발)
  SC 13G  - 5% 보유 (passive)
  SC 13D  - 5% 보유 (active)
  10-K/10-Q - 정기 보고서
  Form 4  - 내부자 거래 (이미 OpenInsider 로 수집 중, skip)
"""
from __future__ import annotations

import logging
from typing import Optional

import requests

logger = logging.getLogger("collectors.us_sec_filings")

_HEADERS = {
    "User-Agent": "JurinMapBot research@example.com",
    "Accept": "application/json",
    "Host": "data.sec.gov",
}

# CIK 매핑 (us_sec_facts 와 별도 캐시)
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


# Dilution risk score 산정 시 가중치 (페니 단타 관점)
DILUTION_WEIGHTS = {
    "424B5": 3.0,   # 실제 offering 발표 — 가장 강한 dilution 신호
    "424B4": 3.0,
    "424B2": 2.5,
    "424B3": 2.0,
    "S-1": 2.0,     # 신규 등록 — 즉시 dilution 아니지만 예고
    "S-1/A": 2.0,
    "S-3": 1.5,     # secondary offering 등록 (shelf)
    "S-3/A": 1.5,
    "F-1": 2.0,
    "F-3": 1.5,
    "POS AM": 1.5,  # post-effective amendment
}

# Filing subtype 분류 — primary_doc_desc + items 텍스트 매칭.
# 페니 단타 관점에서 다른 위험 수준 표시.
DILUTION_SUBTYPES = {
    "atm":              ("ATM 발행",        "red",    "At-the-Market 발행 — 시세에 따라 무한 dilution"),
    "registered_direct": ("Registered Direct", "red",  "기관에 직접 발행 — 즉시 dilution"),
    "pipe":             ("PIPE 거래",        "red",    "사모투자 — 종종 큰 할인"),
    "convertible":      ("전환사채",         "orange", "주식 전환 시 dilution"),
    "warrant":          ("워런트 발행",       "orange", "행사 시 추가 dilution"),
    "reverse_split":    ("주식병합",          "red",    "보통 살아남기 위한 발버둥 — 직후 추가 발행 빈번"),
    "going_concern":    ("계속기업 의문",     "red",    "회계법인의 회사 존속 의문 표시"),
    "delisting_risk":   ("상장폐지 위험",     "red",    "Nasdaq/NYSE 상장 요건 미달 통지"),
    "shelf":            ("Shelf 등록",       "yellow", "최대 N년 내 추가 발행 가능"),
    "general_offering": ("일반 발행",         "orange", "secondary offering"),
}


def classify_filing_subtype(form: str, primary_doc_desc: str, items: str) -> str | None:
    """form + desc + items 로 subtype 분류.

    텍스트 매칭 우선순위 (가장 specific → 가장 generic).
    """
    desc = (primary_doc_desc or "").lower()
    items_str = (items or "").lower()
    form_u = (form or "").upper()

    # Reverse split — 8-K Item 5.03 (정관 변경) + reverse 키워드
    if "reverse" in desc and ("split" in desc or "stock split" in desc):
        return "reverse_split"
    if "5.03" in items_str and "reverse" in desc:
        return "reverse_split"

    # Going concern — 10-K/10-Q 의 going concern 경고
    if "going concern" in desc:
        return "going_concern"

    # Delisting risk — 8-K Item 3.01
    if "3.01" in items_str:
        return "delisting_risk"
    if any(kw in desc for kw in ("delisting", "minimum bid price", "listing rule", "listing requirement")):
        return "delisting_risk"

    # ATM offering — 424B5 가장 흔함
    if "at-the-market" in desc or "at the market" in desc or "atm offering" in desc:
        return "atm"
    if " atm " in f" {desc} " and "offering" in desc:
        return "atm"

    # Registered direct
    if "registered direct" in desc:
        return "registered_direct"

    # PIPE
    if "private investment" in desc or "pipe " in f"{desc} " or "private placement" in desc:
        return "pipe"

    # Convertible
    if "convertible" in desc and ("note" in desc or "debenture" in desc or "bond" in desc or "preferred" in desc):
        return "convertible"

    # Warrant
    if "warrant" in desc:
        return "warrant"

    # Shelf registration — S-3 일반 형태
    if form_u in ("S-3", "S-3/A", "F-3") and "shelf" in desc:
        return "shelf"
    if form_u in ("S-3", "S-3/A", "F-3") and ("registration" in desc or "prospectus" in desc):
        return "shelf"

    # 일반 secondary offering — 424B 시리즈 default
    if form_u.startswith("424"):
        return "general_offering"
    if form_u in ("S-1", "S-1/A", "F-1") and ("offering" in desc or "prospectus" in desc):
        return "general_offering"

    return None

# AI 요약 우선 대상 form
SUMMARY_FORMS = {"8-K", "6-K"}

# 추적 대상 form 전체 (다른 건 무시)
TRACKED_FORMS = (
    set(DILUTION_WEIGHTS.keys())
    | SUMMARY_FORMS
    | {"10-K", "10-Q", "20-F", "SC 13G", "SC 13D", "SC 13G/A", "SC 13D/A"}
)


def _accession_url(cik: str, accession: str) -> str:
    """EDGAR filing 페이지 URL."""
    acc_clean = accession.replace("-", "")
    return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=&dateb=&owner=include&count=40"


def _document_url(cik: str, accession: str, primary_doc: str) -> str:
    """primary document 직링크."""
    acc_clean = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{primary_doc}"


def fetch_sec_submissions(symbol: str, max_rows: int = 200) -> Optional[dict]:
    """최근 filings 가져오기. cik + entity_name + filings list 반환."""
    _load_cik_mapping()
    cik = _CIK_CACHE.get(symbol.upper())
    if not cik:
        return None

    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        logger.debug("SEC submissions failed (%s): %s", symbol, exc)
        return None

    recent = data.get("filings", {}).get("recent", {}) or {}
    forms = recent.get("form", []) or []
    if not forms:
        return None

    filings: list[dict] = []
    rows = min(len(forms), max_rows)
    for i in range(rows):
        form = forms[i]
        if form not in TRACKED_FORMS:
            continue
        accession = (recent.get("accessionNumber") or [""])[i] if i < len(recent.get("accessionNumber") or []) else ""
        filing_date = (recent.get("filingDate") or [""])[i] if i < len(recent.get("filingDate") or []) else ""
        primary_doc = (recent.get("primaryDocument") or [""])[i] if i < len(recent.get("primaryDocument") or []) else ""
        primary_desc = (recent.get("primaryDocDescription") or [""])[i] if i < len(recent.get("primaryDocDescription") or []) else ""
        items_str = (recent.get("items") or [""])[i] if i < len(recent.get("items") or []) else ""
        report_date = (recent.get("reportDate") or [""])[i] if i < len(recent.get("reportDate") or []) else ""
        size = (recent.get("size") or [0])[i] if i < len(recent.get("size") or []) else 0

        subtype = classify_filing_subtype(form, primary_desc, items_str)
        filings.append({
            "form": form,
            "accession": accession,
            "filing_date": filing_date,
            "report_date": report_date or None,
            "primary_doc": primary_doc,
            "primary_doc_desc": primary_desc,
            "items": items_str or None,  # 8-K: "1.01,8.01" 형식
            "size": size,
            "doc_url": _document_url(cik, accession, primary_doc) if accession and primary_doc else None,
            "is_dilution": form in DILUTION_WEIGHTS,
            "is_summary_target": form in SUMMARY_FORMS,
            "subtype": subtype,
        })

    return {
        "symbol": symbol.upper(),
        "cik": cik,
        "entity_name": data.get("name"),
        "sic": data.get("sic"),
        "sic_description": data.get("sicDescription"),
        "filings": filings,
        "total_tracked": len(filings),
    }


def fetch_filing_text(doc_url: str, max_chars: int = 30000) -> Optional[str]:
    """8-K 본문 텍스트 가져오기 — AI 요약 입력용.

    SEC 는 HTML 또는 .htm 으로 제공. 단순 텍스트 추출.
    max_chars 로 제한 — 8-K 보통 5-50KB.
    """
    if not doc_url:
        return None
    try:
        r = requests.get(doc_url, headers={"User-Agent": "JurinMapBot research@example.com"}, timeout=30)
        r.raise_for_status()
        text = r.text
    except Exception as exc:
        logger.debug("fetch filing text failed: %s", exc)
        return None

    # 매우 단순한 HTML 텍스트 추출 (외부 lib 없이)
    import re
    # script / style 제거
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    # tag 제거
    text = re.sub(r"<[^>]+>", " ", text)
    # html entity decode (간단히)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"")
    # whitespace 정리
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def dilution_risk_score(filings: list[dict], window_days: int = 180) -> dict:
    """최근 N일 dilution 관련 filing 누적 점수.

    score >= 5 : 심각 (red)
    3 ~ 5     : 주의 (orange)
    1 ~ 3     : 관찰 (yellow)
    0         : 깨끗
    """
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    score = 0.0
    counts: dict[str, int] = {}
    latest_offering = None  # 424B5 / S-1 가장 최근
    for f in filings:
        fd = f.get("filing_date") or ""
        if fd < cutoff:
            continue
        form = f.get("form")
        w = DILUTION_WEIGHTS.get(form)
        if not w:
            continue
        score += w
        counts[form] = counts.get(form, 0) + 1
        # 가장 최근 dilution
        if form in ("424B5", "424B4", "424B2", "S-1", "F-1"):
            if not latest_offering or fd > latest_offering.get("filing_date", ""):
                latest_offering = f

    if score >= 5:
        tier = "심각"
        tier_color = "red"
    elif score >= 3:
        tier = "주의"
        tier_color = "orange"
    elif score >= 1:
        tier = "관찰"
        tier_color = "yellow"
    else:
        tier = "깨끗"
        tier_color = "gray"

    return {
        "score": round(score, 1),
        "tier": tier,
        "tier_color": tier_color,
        "window_days": window_days,
        "counts": counts,
        "latest_offering": latest_offering,
        "total_dilution_filings": sum(counts.values()),
    }


if __name__ == "__main__":
    import sys
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    syms = sys.argv[1:] if len(sys.argv) > 1 else ["NVDA", "ALP", "WOK"]
    for sym in syms:
        f = fetch_sec_submissions(sym)
        if not f:
            print(f"\n{sym}: (CIK 없음 또는 submissions 없음)")
            continue
        print(f"\n=== {sym} ({f['entity_name']}) — total {len(f['filings'])} tracked filings ===")
        risk = dilution_risk_score(f["filings"])
        print(f"  Dilution risk: {risk['score']} ({risk['tier']}) · 6개월 내 {risk['total_dilution_filings']}건 · {risk['counts']}")
        if risk["latest_offering"]:
            lo = risk["latest_offering"]
            print(f"  Latest offering: {lo['form']} on {lo['filing_date']} — {lo.get('primary_doc_desc')}")
        print("  Recent 10:")
        for filing in f["filings"][:10]:
            items = f" [Items: {filing['items']}]" if filing.get("items") else ""
            print(f"    {filing['filing_date']} {filing['form']:<10} {filing['primary_doc_desc'] or filing['primary_doc']}{items}")
