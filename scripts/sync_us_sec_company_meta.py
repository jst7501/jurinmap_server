"""SEC EDGAR submissions 메타 sync — us_stocks 보강.

SIC code, former names, state of incorporation, fiscal year, EIN, phone, business address 등
yfinance 가 안 주는 데이터. 페니 트레이더에게 reverse merger / shell 회사 추적 핵심.

Usage:
  python scripts/sync_us_sec_company_meta.py --penny-only
  python scripts/sync_us_sec_company_meta.py --symbols ALP,WOK
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

from collectors.us_sec_company_meta import fetch_sec_meta  # noqa: E402
from server.db.connections import get_stocks_conn  # noqa: E402

logger = logging.getLogger("scripts.sync_us_sec_company_meta")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


COLUMNS = [
    ("sec_cik", "TEXT"),
    ("sec_name", "TEXT"),
    ("sic_code", "TEXT"),
    ("sic_description", "TEXT"),
    ("state_of_incorporation", "TEXT"),
    ("fiscal_year_end", "TEXT"),
    ("filer_category", "TEXT"),
    ("ein", "TEXT"),
    ("phone", "TEXT"),
    ("business_address", "TEXT"),
    ("investor_website", "TEXT"),
    ("former_names_json", "TEXT"),
    ("sec_exchanges_json", "TEXT"),
    ("sec_meta_updated_at", "TIMESTAMP"),
]


def _ensure_columns(conn) -> None:
    for col, typ in COLUMNS:
        try:
            conn.execute(f"ALTER TABLE us_stocks ADD COLUMN IF NOT EXISTS {col} {typ}")
        except Exception as exc:
            logger.debug("alter %s: %s", col, exc)
    try:
        conn.commit()
    except Exception:
        pass


def _select_targets(conn, penny_only: bool, symbols: list[str], refetch_stale_days: int, limit: int):
    if symbols:
        return symbols
    where = []
    if penny_only:
        where.append("is_penny = TRUE")
    where.append("exchange IN ('NASDAQ','NYSE','NYSE_AMEX')")
    where.append("(is_etf = FALSE OR is_etf IS NULL)")
    if refetch_stale_days > 0:
        where.append(
            f"(sec_meta_updated_at IS NULL OR sec_meta_updated_at < NOW() - INTERVAL '{int(refetch_stale_days)} days')"
        )
    where_sql = " AND ".join(where) if where else "1=1"
    sql = f"SELECT ticker FROM us_stocks WHERE {where_sql} ORDER BY is_penny DESC NULLS LAST, market_cap_usd ASC NULLS LAST"
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    cur = conn.execute(sql)
    return [r[0] for r in cur.fetchall() if r[0]]


def sync(targets: list[str], delay: float = 0.12) -> tuple[int, int]:
    if not targets:
        return 0, 0
    print(f"[sync_us_sec_company_meta] {len(targets)} tickers, delay={delay}s")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    conn = get_stocks_conn()
    _ensure_columns(conn)
    fetched = 0
    updated = 0

    import time
    for i, sym in enumerate(targets):
        info = fetch_sec_meta(sym)
        fetched += 1
        if info:
            try:
                conn.execute(
                    """
                    UPDATE us_stocks SET
                        sec_cik = %s,
                        sec_name = %s,
                        sic_code = %s,
                        sic_description = %s,
                        state_of_incorporation = %s,
                        fiscal_year_end = %s,
                        filer_category = %s,
                        ein = %s,
                        phone = %s,
                        business_address = %s,
                        investor_website = %s,
                        former_names_json = %s,
                        sec_exchanges_json = %s,
                        sec_meta_updated_at = %s
                    WHERE ticker = %s
                    """,
                    (
                        info.get("cik"),
                        info.get("sec_name"),
                        info.get("sic_code"),
                        info.get("sic_description"),
                        info.get("state_of_incorporation"),
                        info.get("fiscal_year_end"),
                        info.get("filer_category"),
                        info.get("ein"),
                        info.get("phone"),
                        info.get("business_address"),
                        info.get("investor_website"),
                        json.dumps(info.get("former_names") or [], ensure_ascii=False) if info.get("former_names") else None,
                        json.dumps(info.get("exchanges") or []) if info.get("exchanges") else None,
                        now, sym,
                    ),
                )
                updated += 1
            except Exception as exc:
                logger.debug("update %s: %s", sym, exc)
        if (i + 1) % 50 == 0:
            conn.commit()
            print(f"  [{i+1}/{len(targets)}] fetched={fetched} updated={updated}")
        time.sleep(delay)
    conn.commit()
    conn.close()
    return fetched, updated


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--penny-only", action="store_true")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--refetch-stale", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.12)
    args = ap.parse_args()

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] if args.symbols else []
    conn = get_stocks_conn()
    try:
        _ensure_columns(conn)
        targets = _select_targets(conn, args.penny_only, syms, args.refetch_stale, args.limit)
        print(f"[targets] {len(targets)}")
    finally:
        conn.close()
    if not targets:
        return 0
    fetched, updated = sync(targets, delay=args.delay)
    print(f"[done] fetched={fetched} updated={updated}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
