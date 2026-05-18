"""us_stocks.summary 보강.

소스:
  1. yfinance.Ticker.info.longBusinessSummary (페니 50% 미커버)
  2. SEC EDGAR submissions API 의 description (페니 100% 커버, 짧음)
  3. Finnhub /stock/profile2 에는 description 없음

페니 NULL summary 67/134 → 100% 가까이.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

from collectors.us_sec_filings import _load_cik_mapping, _CIK_CACHE  # noqa: E402
from server.db.connections import get_stocks_conn  # noqa: E402

logger = logging.getLogger("scripts.sync_us_summary")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_HEADERS = {"User-Agent": "JurinMapBot research@example.com", "Accept": "application/json", "Host": "data.sec.gov"}


def fetch_yfinance_summary(symbol: str) -> str | None:
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info or {}
        s = info.get("longBusinessSummary") or info.get("description")
        if s and len(s) > 50:
            return s
    except Exception as exc:
        logger.debug("yf summary %s: %s", symbol, exc)
    return None


def fetch_sec_description(cik: str) -> str | None:
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
        # description 또는 그 외 fields 시도
        desc = data.get("description")
        if desc and len(desc) > 30:
            return desc
        # category + sicDescription 조합
        parts = []
        if data.get("category"):
            parts.append(data["category"])
        if data.get("sicDescription"):
            parts.append(data["sicDescription"])
        if data.get("name") and data.get("stateOfIncorporationDescription"):
            parts.append(f"등기 {data['stateOfIncorporationDescription']}")
        if parts:
            return " · ".join(parts)
    except Exception as exc:
        logger.debug("sec submissions %s: %s", cik, exc)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--penny-only", action="store_true", default=True)
    ap.add_argument("--null-only", action="store_true", default=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.15)
    args = ap.parse_args()

    _load_cik_mapping()

    conn = get_stocks_conn()
    where = ["exchange IN ('NASDAQ','NYSE','NYSE_AMEX')", "(is_etf = FALSE OR is_etf IS NULL)", "length(ticker) <= 5"]
    if args.penny_only:
        where.append("is_penny = TRUE")
    if args.null_only:
        where.append("(summary IS NULL OR summary = '' OR length(summary) < 30)")
    sql = f"SELECT ticker FROM us_stocks WHERE {' AND '.join(where)} ORDER BY market_cap_usd DESC NULLS LAST"
    if args.limit > 0:
        sql += f" LIMIT {int(args.limit)}"
    cur = conn.execute(sql)
    targets = [r[0] for r in cur.fetchall() if r and r[0]]
    print(f"[targets] {len(targets)}")
    if not targets:
        return 0

    updated_yf = 0
    updated_sec = 0
    no_data = 0
    for i, sym in enumerate(targets):
        # 1. yfinance 먼저 (긴 summary)
        summary = fetch_yfinance_summary(sym)
        source = "yfinance"
        # 2. yf 실패 → SEC fallback
        if not summary:
            cik = _CIK_CACHE.get(sym)
            if cik:
                summary = fetch_sec_description(cik)
                source = "sec"
        if not summary:
            no_data += 1
            time.sleep(args.delay)
            continue
        try:
            conn.execute("UPDATE us_stocks SET summary = %s WHERE ticker = %s", (summary, sym))
            if source == "yfinance":
                updated_yf += 1
            else:
                updated_sec += 1
        except Exception as exc:
            logger.debug("upsert %s: %s", sym, exc)
        if (i + 1) % 15 == 0:
            try:
                conn.commit()
            except Exception:
                pass
            print(f"  [{i+1}/{len(targets)}] yf={updated_yf} sec={updated_sec} none={no_data}")
        time.sleep(args.delay)

    try:
        conn.commit()
    except Exception:
        pass
    conn.close()
    print(f"[done] yfinance={updated_yf} sec={updated_sec} no_data={no_data}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
