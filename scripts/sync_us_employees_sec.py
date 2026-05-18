"""SEC EDGAR DEI 의 EntityNumberOfEmployees → us_stocks.employees 채움.

페니에서 yfinance 가 못 주는 직원 수를 SEC 가 100% 제공.
페니 134종목 모두 1회 sync 약 5분.
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

logger = logging.getLogger("scripts.sync_us_employees_sec")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_HEADERS = {"User-Agent": "JurinMapBot research@example.com", "Accept": "application/json", "Host": "data.sec.gov"}


def fetch_employees(cik: str) -> int | None:
    """SEC Company Facts API → dei.EntityNumberOfEmployees 최신값."""
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        logger.debug("companyfacts %s: %s", cik, exc)
        return None

    dei = data.get("facts", {}).get("dei", {})
    emp = dei.get("EntityNumberOfEmployees", {})
    units = emp.get("units", {}).get("pure", [])
    if not units:
        return None
    # end 가장 큰 것
    units_sorted = sorted(units, key=lambda r: r.get("end") or "", reverse=True)
    val = units_sorted[0].get("val")
    try:
        return int(val) if val is not None else None
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--penny-only", action="store_true", default=True)
    ap.add_argument("--null-only", action="store_true", default=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.12)
    args = ap.parse_args()

    _load_cik_mapping()

    conn = get_stocks_conn()
    where = ["exchange IN ('NASDAQ','NYSE','NYSE_AMEX')", "(is_etf = FALSE OR is_etf IS NULL)", "length(ticker) <= 5"]
    if args.penny_only:
        where.append("is_penny = TRUE")
    if args.null_only:
        where.append("(employees IS NULL OR employees = 0)")
    sql = f"SELECT ticker FROM us_stocks WHERE {' AND '.join(where)} ORDER BY market_cap_usd DESC NULLS LAST"
    if args.limit > 0:
        sql += f" LIMIT {int(args.limit)}"
    cur = conn.execute(sql)
    targets = [r[0] for r in cur.fetchall() if r and r[0]]
    print(f"[targets] {len(targets)} tickers")
    if not targets:
        return 0

    updated = 0
    no_data = 0
    for i, sym in enumerate(targets):
        cik = _CIK_CACHE.get(sym)
        if not cik:
            no_data += 1
            continue
        emp = fetch_employees(cik)
        if emp is None or emp <= 0:
            no_data += 1
            time.sleep(args.delay)
            continue
        try:
            conn.execute("UPDATE us_stocks SET employees = %s WHERE ticker = %s", (emp, sym))
            updated += 1
        except Exception as exc:
            logger.debug("update %s: %s", sym, exc)
        if (i + 1) % 20 == 0:
            try:
                conn.commit()
            except Exception:
                pass
            print(f"  [{i+1}/{len(targets)}] updated={updated} no_data={no_data}")
        time.sleep(args.delay)

    try:
        conn.commit()
    except Exception:
        pass
    conn.close()
    print(f"[done] updated={updated} no_data={no_data}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
