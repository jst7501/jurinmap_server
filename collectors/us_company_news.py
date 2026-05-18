"""미국 종목 뉴스 수집 — yfinance.news + SEC EDGAR submissions (8-K 등).

소스 2종:
  1. yfinance.Ticker.news — 일반 뉴스 (Yahoo Finance aggregator)
     - title, publisher, link, providerPublishTime, type, relatedTickers
  2. SEC EDGAR submissions/CIK{cik}.json — 공시 (8-K, 10-Q, 10-K, S-1, ...)
     - https://data.sec.gov/submissions/CIK0001045810.json
     - filings.recent.form / filingDate / accessionNumber / primaryDocument

페니에서 가장 중요한 신호:
  - **S-1 / S-3** = 증자 공시 (희석)
  - **8-K** = 중대 사건 (CEO 교체, M&A, 계약 등)
  - **4** = 내부자 거래
  - **DEF 14A** = 위임장
  - **NT 10-Q** = 제출 연기

CIK 찾기: SEC ticker_to_cik file 또는 EDGAR full-text search.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger("collectors.us_company_news")

_HEADERS_YF = {"User-Agent": "Mozilla/5.0 (compatible; JurinMapBot/1.0)"}
_HEADERS_SEC = {
    "User-Agent": "JurinMapBot research@example.com",
    "Accept": "application/json",
    "Host": "data.sec.gov",
}

# SEC ticker → CIK 매핑 (캐시)
_CIK_CACHE: dict[str, str] = {}
_CIK_CACHE_LOADED = False

# 페니 트레이더 관심 form 들
_INTERESTING_FORMS = {
    "8-K": ("중대 사건", "red"),
    "S-1": ("증자/IPO 등록", "red"),
    "S-3": ("증자 등록", "red"),
    "424B5": ("증자 가격 공시", "red"),
    "10-Q": ("분기 실적", "orange"),
    "10-K": ("연 실적", "orange"),
    "4": ("내부자 거래", "purple"),
    "SC 13D": ("5% 이상 매수 공시", "purple"),
    "SC 13G": ("5% 이상 매수 공시 (수동)", "gray"),
    "DEF 14A": ("주주총회 위임장", "gray"),
    "NT 10-Q": ("10-Q 제출 연기", "orange"),
    "NT 10-K": ("10-K 제출 연기", "orange"),
}


def _load_cik_mapping() -> None:
    """SEC ticker → CIK 매핑 한번 로드 (~10K 종목)."""
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
        data = r.json()
        # 구조: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
        for v in data.values():
            t = v.get("ticker")
            cik = v.get("cik_str")
            if t and cik:
                _CIK_CACHE[t.upper()] = str(cik).zfill(10)
        _CIK_CACHE_LOADED = True
        logger.info("SEC ticker→CIK 매핑 로드 완료: %d 종목", len(_CIK_CACHE))
    except Exception as exc:
        logger.warning("SEC ticker_to_cik load failed: %s", exc)


def _ticker_to_cik(ticker: str) -> Optional[str]:
    _load_cik_mapping()
    return _CIK_CACHE.get(ticker.upper())


def fetch_yfinance_news(symbol: str, limit: int = 20) -> list[dict]:
    """yfinance.news — Yahoo Finance aggregator 뉴스 (~10-20개)."""
    try:
        import yfinance as yf
        news = yf.Ticker(symbol).news or []
    except Exception as exc:
        logger.debug("yfinance news fetch failed (%s): %s", symbol, exc)
        return []

    out = []
    for n in news[:limit]:
        if not isinstance(n, dict):
            continue
        # yfinance 응답 구조가 가변 — content 가 nested 일 수 있음
        c = n.get("content") if isinstance(n.get("content"), dict) else n
        title = (c.get("title") or n.get("title") or "").strip()
        if not title:
            continue
        published_ts = c.get("pubDate") or c.get("displayTime") or n.get("providerPublishTime")
        # ISO string 또는 epoch
        published_at = None
        try:
            if isinstance(published_ts, str):
                # "2026-05-14T10:00:00Z" 같은 ISO
                published_at = datetime.fromisoformat(published_ts.replace("Z", "+00:00"))
            elif isinstance(published_ts, (int, float)) and published_ts > 0:
                published_at = datetime.fromtimestamp(published_ts, tz=timezone.utc)
        except Exception:
            published_at = None

        url = ""
        if isinstance(c.get("clickThroughUrl"), dict):
            url = c["clickThroughUrl"].get("url") or ""
        elif isinstance(c.get("canonicalUrl"), dict):
            url = c["canonicalUrl"].get("url") or ""
        url = url or c.get("link") or n.get("link") or ""

        publisher = ""
        if isinstance(c.get("provider"), dict):
            publisher = c["provider"].get("displayName") or ""
        publisher = publisher or c.get("publisher") or n.get("publisher") or ""

        out.append({
            "symbol": symbol.upper(),
            "source": "yfinance",
            "form_type": None,
            "title": title[:300],
            "publisher": publisher[:100] or None,
            "url": url[:500] or None,
            "published_at": published_at,
            "summary": (c.get("summary") or "")[:500] or None,
        })
    return out


def fetch_sec_filings(symbol: str, limit: int = 20) -> list[dict]:
    """SEC EDGAR submissions API — 최근 공시 (8-K, 10-Q, S-1, 등)."""
    cik = _ticker_to_cik(symbol)
    if not cik:
        return []
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        r = requests.get(url, headers=_HEADERS_SEC, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        logger.debug("SEC submissions fetch failed (%s/%s): %s", symbol, cik, exc)
        return []

    recent = data.get("filings", {}).get("recent", {})
    if not recent:
        return []

    forms = recent.get("form", []) or []
    dates = recent.get("filingDate", []) or []
    accessions = recent.get("accessionNumber", []) or []
    primary_docs = recent.get("primaryDocument", []) or []
    descriptions = recent.get("primaryDocDescription", []) or []

    out = []
    for i, form in enumerate(forms[:limit * 3]):  # 더 많이 시도해서 interesting 으로 필터
        if not form:
            continue
        # 관심 form 만 (또는 모두 — 일단 다 받고 frontend 에서 필터)
        filing_date = dates[i] if i < len(dates) else None
        accession = accessions[i] if i < len(accessions) else ""
        primary_doc = primary_docs[i] if i < len(primary_docs) else ""
        desc = descriptions[i] if i < len(descriptions) else ""

        # EDGAR URL — accession 에서 - 제거 후 /Archives/edgar/data/{cik}/{accession-no-dashes}/
        accession_nodash = accession.replace("-", "")
        url_filing = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{accession_nodash}/{primary_doc}"
            if primary_doc else
            f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form}"
        )

        published_at = None
        if filing_date:
            try:
                published_at = datetime.fromisoformat(filing_date).replace(tzinfo=timezone.utc)
            except Exception:
                published_at = None

        meta = _INTERESTING_FORMS.get(form)
        title = f"[{form}] {meta[0] if meta else form}"
        if desc:
            title += f" — {desc[:80]}"

        out.append({
            "symbol": symbol.upper(),
            "source": "sec_edgar",
            "form_type": form,
            "title": title[:300],
            "publisher": "SEC",
            "url": url_filing[:500],
            "published_at": published_at,
            "summary": None,
        })
        if len([x for x in out if x["source"] == "sec_edgar"]) >= limit:
            break

    return out


def fetch_finnhub_news(symbol: str, days: int = 30) -> list[dict]:
    """Finnhub /company-news — 페니에서 yfinance.news 보다 신뢰성 ↑."""
    try:
        from collectors.us_finnhub import get_company_news
        rows = get_company_news(symbol, days=days)
    except Exception as exc:
        logger.debug("finnhub news %s: %s", symbol, exc)
        return []
    out = []
    for r in rows:
        if not r.get("headline") or not r.get("url"):
            continue
        published_at = None
        ts = r.get("datetime")
        if ts:
            try:
                published_at = datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None)
            except Exception:
                pass
        out.append({
            "symbol": symbol.upper(),
            "source": "finnhub",
            "url": r["url"],
            "title": r["headline"],
            "publisher": r.get("source"),
            "summary": r.get("summary") or "",
            "form_type": r.get("category"),
            "published_at": published_at,
        })
    return out


def _normalize_dt(d):
    """tz-aware → naive UTC. None 유지."""
    if d is None:
        return None
    try:
        if hasattr(d, "tzinfo") and d.tzinfo is not None:
            return d.astimezone(timezone.utc).replace(tzinfo=None)
        return d
    except Exception:
        return None


def fetch_all_news(symbol: str, yf_limit: int = 15, sec_limit: int = 15, finnhub_days: int = 30) -> list[dict]:
    """Finnhub + yfinance + SEC 통합. 시각 내림차순 + dedupe (url).

    published_at 은 모두 naive UTC datetime 로 통일.
    """
    finnhub_news = fetch_finnhub_news(symbol, days=finnhub_days)
    yf_news = fetch_yfinance_news(symbol, yf_limit)
    sec_news = fetch_sec_filings(symbol, sec_limit)
    all_news = finnhub_news + yf_news + sec_news

    # datetime tz 통일 (tz-aware ↔ naive 비교 에러 회피)
    for n in all_news:
        n["published_at"] = _normalize_dt(n.get("published_at"))

    # dedupe by url (Finnhub 가 우선이라 먼저 들어감)
    seen = set()
    deduped = []
    for n in all_news:
        key = n.get("url") or n["title"]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(n)

    # 시각 내림차순 (None 은 끝으로)
    try:
        deduped.sort(key=lambda x: (x["published_at"] is None, x["published_at"]), reverse=True)
        deduped = [n for n in deduped if n["published_at"] is not None] + [n for n in deduped if n["published_at"] is None]
    except Exception as exc:
        logger.debug("sort error: %s", exc)
    return deduped


if __name__ == "__main__":
    import sys
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    news = fetch_all_news(sym)
    print(f"\n{sym} 뉴스 + 공시 {len(news)} 건:")
    for n in news[:15]:
        src = n["source"][:8]
        form = n.get("form_type") or ""
        pub_at = n["published_at"].strftime("%m/%d %H:%M") if n["published_at"] else "-"
        print(f"  [{src:8}] {pub_at} {form:6} {n['title'][:80]}")
