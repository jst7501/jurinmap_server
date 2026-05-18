"""미국 종목의 한글명 — 네이버 금융 autocomplete API.

네이버가 미국 메이저 종목 ~3000개에 한글 회사명 부여 (엔비디아, 테슬라 등).
reutersCode 정확 매칭으로 false positive 방지.

API: https://ac.stock.naver.com/ac?q={SYMBOL}&target=stock,index,marketindicator
응답 items[*].reutersCode = "NVDA.O" 같은 형식. base 가 ticker 와 일치하면 한글명 채택.

Rate limit 미공개 — 보수적으로 0.15초/요청. 12000개 = ~30분.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger("collectors.us_korean_names")

_AC_URL = "https://ac.stock.naver.com/ac"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JurinMapBot/1.0)",
    "Referer": "https://stock.naver.com/",
}


def fetch_korean_name(symbol: str, timeout: float = 8.0) -> Optional[str]:
    """단일 ticker → 한글 회사명. 매칭 안 되면 None."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    try:
        r = requests.get(
            _AC_URL,
            params={"q": sym, "target": "stock,index,marketindicator"},
            headers=_HEADERS,
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        items = r.json().get("items", [])
    except Exception as exc:
        logger.debug("naver ac failed (%s): %s", sym, exc)
        return None

    # reutersCode 정확 매칭 — "NVDA.O" / "AAPL.O" / "BRK.A" 등
    for item in items:
        reuters = (item.get("reutersCode") or "").strip()
        if not reuters:
            continue
        base = reuters.split(".")[0]
        if base.upper() == sym:
            name = item.get("name") or ""
            return name.strip() or None
    return None


def batch_fetch(symbols: list[str], delay: float = 0.15) -> dict[str, Optional[str]]:
    """ticker list → {ticker: 한글명 또는 None} dict.

    delay: 요청 사이 sleep (rate limit 보호, 기본 0.15초).
    """
    out: dict[str, Optional[str]] = {}
    for i, sym in enumerate(symbols):
        out[sym] = fetch_korean_name(sym)
        if delay > 0 and i < len(symbols) - 1:
            time.sleep(delay)
    return out


if __name__ == "__main__":
    import sys
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    syms = sys.argv[1:] if len(sys.argv) > 1 else [
        "NVDA", "AMD", "TSLA", "COST", "PATH", "RKLB", "AAPL", "MU", "META", "MSFT",
        "SPOT", "PLTR", "BTC", "ETH", "XYZ123",
    ]
    results = batch_fetch(syms)
    for sym, name in results.items():
        print(f"  {sym:8} → {name or '(매칭 없음)'}")
