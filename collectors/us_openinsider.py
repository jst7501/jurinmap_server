"""OpenInsider 스크래핑 — Form 4 내부자 거래 정리 데이터.

URL: http://openinsider.com/screener?s={ticker}&...
HTML 테이블 파싱. yfinance / EDGAR 가 raw form 4 만 주는 반면 OpenInsider 는 정리된 사람·금액·종류.

핵심 컬럼:
  filing_date / trade_date — YYYY-MM-DD
  insider_name / title       — 이름 + 직책 (CEO/CFO/Dir/10% Owner 등)
  trade_type                 — P (Purchase) / S (Sale) / A (Award) / D (Disposition)
  price                      — 거래 가격
  qty                        — 거래 주식수 (매도시 음수)
  owned_after                — 거래 후 보유
  delta_own_pct              — 보유 변화 %
  value                      — 총 거래 가치 ($)

페니 시그널:
  - Cluster Buy = 여러 임원이 짧은 시간 내 P (매수) — 강한 bullish
  - 10% Owner 의 P  — Reg 13D filing 이전 알림
  - 임원 S (매도) cluster — bearish
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger("collectors.us_openinsider")

_BASE = "http://openinsider.com/screener"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JurinMapBot/1.0)"}


def fetch_insider_trades(symbol: str, days_back: int = 730, limit: int = 100) -> list[dict]:
    """단일 종목의 내부자 거래 list. 최근 N일치."""
    params = {
        "s": symbol.upper(),
        "fd": str(days_back),   # filing date ~N일 전부터
        "xp": "1", "xs": "1",   # Purchase + Sale 모두
        "sortcol": "0",          # 날짜 내림차순
        "cnt": str(min(limit, 200)),
        "page": "1",
        "sic1": "-1",
    }
    try:
        r = requests.get(_BASE, params=params, headers=_HEADERS, timeout=20)
        r.raise_for_status()
    except Exception as exc:
        logger.debug("openinsider fetch failed (%s): %s", symbol, exc)
        return []

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("bs4 not installed")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table", class_="tinytable")
    if not table:
        return []
    body = table.find("tbody")
    if not body:
        return []

    out = []
    for row in body.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 13:
            continue
        texts = [c.get_text(strip=True) for c in cells]
        # 0: filing flag (X/CL/...)
        # 1: Filing Date "2026-03-20 12:34:56"
        # 2: Trade Date "2026-03-19"
        # 3: Ticker
        # 4: Insider Name
        # 5: Title
        # 6: Trade Type — "S - Sale" / "P - Purchase" / "A - Award"
        # 7: Price
        # 8: Qty
        # 9: Owned
        # 10: ΔOwn (%)
        # 11: Value
        try:
            trade_type_raw = texts[6]
            tt = "?"
            if "Purchase" in trade_type_raw or trade_type_raw.startswith("P"):
                tt = "P"
            elif "Sale" in trade_type_raw or trade_type_raw.startswith("S"):
                tt = "S"
            elif "Award" in trade_type_raw or trade_type_raw.startswith("A"):
                tt = "A"
            elif "Disposition" in trade_type_raw or trade_type_raw.startswith("D"):
                tt = "D"

            # 숫자 파싱 — "$173.68" / "-221,682" / "+5.32%" / "-$38,502,524"
            def _num(s: str) -> Optional[float]:
                s = (s or "").replace(",", "").replace("$", "").replace("%", "").strip()
                if not s or s in ("", "-", "—", "+"):
                    return None
                try:
                    return float(s)
                except ValueError:
                    return None

            filing_date_raw = texts[1][:10]  # "YYYY-MM-DD"
            trade_date = texts[2][:10]
            insider_name = texts[4][:120]
            title = texts[5][:120]
            price = _num(texts[7])
            qty = _num(texts[8])
            owned_after = _num(texts[9])
            delta_own_pct = _num(texts[10])
            value = _num(texts[11])

            out.append({
                "symbol": symbol.upper(),
                "filing_date": filing_date_raw,
                "trade_date": trade_date,
                "insider_name": insider_name,
                "title": title,
                "trade_type": tt,
                "trade_type_raw": trade_type_raw[:30],
                "price": price,
                "qty": qty,
                "owned_after": owned_after,
                "delta_own_pct": delta_own_pct,
                "value": value,
            })
        except Exception as exc:
            logger.debug("row parse failed: %s", exc)
            continue
    return out


def summarize(trades: list[dict]) -> dict:
    """집계: 최근 30일 매수/매도 합계 + cluster 감지."""
    if not trades:
        return {"total": 0, "p_30d_count": 0, "s_30d_count": 0, "p_30d_value": 0, "s_30d_value": 0,
                "cluster_buy": False, "cluster_sell": False}
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    recent = [t for t in trades if (t.get("trade_date") or "") >= cutoff]
    p_30d = [t for t in recent if t["trade_type"] == "P"]
    s_30d = [t for t in recent if t["trade_type"] == "S"]
    p_value = sum((t.get("value") or 0) for t in p_30d)
    s_value = sum(abs(t.get("value") or 0) for t in s_30d)
    # cluster: 30일 내 3명 이상 다른 insider 가 같은 방향 거래
    p_insiders = len({t["insider_name"] for t in p_30d if t.get("insider_name")})
    s_insiders = len({t["insider_name"] for t in s_30d if t.get("insider_name")})
    return {
        "total": len(trades),
        "p_30d_count": len(p_30d),
        "s_30d_count": len(s_30d),
        "p_30d_insiders": p_insiders,
        "s_30d_insiders": s_insiders,
        "p_30d_value": int(p_value),
        "s_30d_value": int(s_value),
        "cluster_buy": p_insiders >= 3,
        "cluster_sell": s_insiders >= 3,
    }


if __name__ == "__main__":
    import sys
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    syms = sys.argv[1:] if len(sys.argv) > 1 else ["NVDA", "ALP", "WOK"]
    for sym in syms:
        trades = fetch_insider_trades(sym)
        s = summarize(trades)
        print(f"\n=== {sym} === ({len(trades)} trades, 30d: P {s['p_30d_count']} S {s['s_30d_count']})")
        if s["cluster_buy"]:
            print(f"  ⚠ CLUSTER BUY {s['p_30d_insiders']}명 ${s['p_30d_value']/1e6:.1f}M")
        if s["cluster_sell"]:
            print(f"  ⚠ CLUSTER SELL {s['s_30d_insiders']}명 ${s['s_30d_value']/1e6:.1f}M")
        for t in trades[:5]:
            print(f"  {t['trade_date']} {t['trade_type']:3} {t['insider_name'][:25]:25} {(t['title'] or '')[:20]:20} ${t['price']} x {t['qty']:,.0f} = ${(t['value'] or 0)/1e3:.0f}K")
