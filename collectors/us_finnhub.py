"""Finnhub free tier (60 calls/min) collector.

yfinance 가 페니에서 자주 빈 응답 → Finnhub fallback.

페니 메타 보강:
  /stock/profile2 — sector, industry, marketCapitalization, shareOutstanding,
                    employeeTotal, weburl, ipo, country, phone
  /quote          — c (current), h (high), l (low), o (open), pc (prev close),
                    t (timestamp). 광고 사이트 사용 가능 라이선스 (Finnhub TOS).
  /stock/insider-transactions (5분 캐시) — 추가 보강용

API key 발급: https://finnhub.io/dashboard
환경변수 FINNHUB_API_KEY 설정 (또는 server/.env)
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger("collectors.us_finnhub")

# .env 자동 로드 — script entry point 가 별도로 dotenv 호출 안 해도 됨
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    pass

_BASE = "https://finnhub.io/api/v1"

# 글로벌 token bucket — Finnhub free 60/min. ThreadPool 어디서든 자동 throttle.
# 분당 60 → 1초 1콜. 약간 여유로 1.05초 간격.
_RATE_LOCK = threading.Lock()
_LAST_CALL_TS = 0.0
_MIN_INTERVAL_SEC = 1.05


def _throttle():
    global _LAST_CALL_TS
    with _RATE_LOCK:
        now = time.time()
        elapsed = now - _LAST_CALL_TS
        if elapsed < _MIN_INTERVAL_SEC:
            wait = _MIN_INTERVAL_SEC - elapsed
            time.sleep(wait)
        _LAST_CALL_TS = time.time()


def _get_api_key() -> str:
    """매 호출마다 환경변수 재조회 (dotenv 늦게 로드된 경우 대응)."""
    return os.getenv("FINNHUB_API_KEY", "").strip()


# legacy compatibility
_API_KEY = _get_api_key()


def _get(path: str, params: dict, timeout: int = 15) -> Optional[dict]:
    key = _get_api_key()
    if not key:
        logger.warning("FINNHUB_API_KEY 미설정")
        return None
    params = {**params, "token": key}
    # 글로벌 throttle — 모든 호출 1.05초 간격 보장 (ThreadPool 어디서든)
    _throttle()
    try:
        r = requests.get(_BASE + path, params=params, timeout=timeout)
        if r.status_code == 429:
            # rate limit 걸리면 백오프 + 재시도 1회
            logger.debug("Finnhub 429, backoff 2s")
            time.sleep(2.0)
            _throttle()
            r = requests.get(_BASE + path, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        logger.debug("finnhub GET %s %s: %s", path, params, exc)
        return None


def get_company_profile(symbol: str) -> Optional[dict]:
    """profile2 — 페니 메타 (sector, industry, employees, ipo, weburl, phone).

    응답 예:
      {"country":"US","currency":"USD","exchange":"NASDAQ",
       "ipo":"2021-10-15","marketCapitalization":12.34,"name":"...",
       "phone":"...", "shareOutstanding":12.3,"ticker":"ALP",
       "weburl":"...", "logo":"...", "finnhubIndustry":"Technology"}
    """
    data = _get("/stock/profile2", {"symbol": symbol.upper()})
    if not data or not isinstance(data, dict) or not data.get("ticker"):
        return None
    return {
        "symbol": data.get("ticker"),
        "name": data.get("name"),
        "exchange": data.get("exchange"),
        "country": data.get("country"),
        "currency": data.get("currency"),
        "ipo": data.get("ipo"),
        "phone": data.get("phone"),
        "weburl": data.get("weburl"),
        "logo": data.get("logo"),
        "industry": data.get("finnhubIndustry"),
        "market_cap_musd": data.get("marketCapitalization"),  # $M 단위
        "shares_outstanding_m": data.get("shareOutstanding"),  # M주 단위
    }


def get_quote(symbol: str) -> Optional[dict]:
    """quote — 페니에서 yfinance 보다 안정적. 지연 ~15분.

    응답:
      {"c":110.0,"d":1.5,"dp":1.39,"h":111,"l":108,"o":109,"pc":108.5,"t":1...}
        c=current, d=change, dp=change%, h=high, l=low, o=open, pc=prev close, t=unix
    """
    data = _get("/quote", {"symbol": symbol.upper()})
    if not data or not isinstance(data, dict) or data.get("c") in (None, 0):
        return None
    return {
        "symbol": symbol.upper(),
        "current_price": data.get("c"),
        "change_amt": data.get("d"),
        "change_pct": data.get("dp"),
        "high": data.get("h"),
        "low": data.get("l"),
        "open_price": data.get("o"),
        "prev_close": data.get("pc"),
        "unix_ts": data.get("t"),
    }


def get_insider_transactions(symbol: str, limit: int = 50) -> list[dict]:
    """insider-transactions — Form 4. OpenInsider 보강용."""
    data = _get("/stock/insider-transactions", {"symbol": symbol.upper(), "limit": limit})
    if not data or "data" not in data:
        return []
    out = []
    for r in data.get("data", []):
        out.append({
            "name": r.get("name"),
            "share": r.get("share"),
            "change": r.get("change"),
            "filingDate": r.get("filingDate"),
            "transactionDate": r.get("transactionDate"),
            "transactionCode": r.get("transactionCode"),
            "transactionPrice": r.get("transactionPrice"),
        })
    return out


def get_company_news(symbol: str, days: int = 30) -> list[dict]:
    """company news — Finnhub 무료, 30일치 페니 catalyst 추적.

    응답:
      [{"category":"company news","datetime":...,"headline":"...",
        "summary":"...","source":"...","url":"...","related":"ALP"}]
    """
    from datetime import datetime as _dt, timedelta as _td
    to_date = _dt.utcnow().strftime("%Y-%m-%d")
    from_date = (_dt.utcnow() - _td(days=days)).strftime("%Y-%m-%d")
    data = _get("/company-news", {"symbol": symbol.upper(), "from": from_date, "to": to_date})
    if not data or not isinstance(data, list):
        return []
    out = []
    for n in data:
        out.append({
            "id": n.get("id"),
            "headline": n.get("headline"),
            "summary": n.get("summary"),
            "url": n.get("url"),
            "source": n.get("source"),
            "category": n.get("category"),
            "datetime": n.get("datetime"),  # unix ts
            "image": n.get("image"),
            "related": n.get("related"),
        })
    return out


def get_recommendation(symbol: str) -> Optional[dict]:
    """애널리스트 평가 (페니 대부분 미커버, 일부 보강)."""
    data = _get("/stock/recommendation", {"symbol": symbol.upper()})
    if not data or not isinstance(data, list) or not data:
        return None
    latest = data[0]
    return {
        "buy": latest.get("buy", 0),
        "hold": latest.get("hold", 0),
        "sell": latest.get("sell", 0),
        "strongBuy": latest.get("strongBuy", 0),
        "strongSell": latest.get("strongSell", 0),
        "period": latest.get("period"),
    }


if __name__ == "__main__":
    import sys
    import io as _io
    import json
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if not _API_KEY:
        print("FINNHUB_API_KEY 환경변수 미설정")
        print("https://finnhub.io/dashboard 에서 무료 발급 후:")
        print("  export FINNHUB_API_KEY=xxxxx")
        sys.exit(1)
    syms = sys.argv[1:] if len(sys.argv) > 1 else ["ALP", "WOK", "AAPL"]
    for sym in syms:
        print(f"\n=== {sym} ===")
        p = get_company_profile(sym)
        print(f"profile: {json.dumps(p, ensure_ascii=False, indent=2) if p else 'None'}")
        q = get_quote(sym)
        print(f"quote: {q}")
