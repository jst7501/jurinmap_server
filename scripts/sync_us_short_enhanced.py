"""us_short_interest_daily 보강 sync — yfinance + stockanalysis 통합.

신규 필드: shares_short_prior_month, short_change_mom_pct, date_short_interest, net_borrowing.

페니 핵심: SI 급증 (+30%+) = squeeze 후보, SI 급감 (-30%) = 공매도 청산 / 약세.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

from collectors.us_short_enhanced import fetch_short_all  # noqa: E402
from server.db.connections import get_stocks_conn  # noqa: E402

logger = logging.getLogger("scripts.sync_us_short_enhanced")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _ensure_table(conn) -> None:
    """us_short_interest_daily 가 이미 있다고 가정. 신규 컬럼만 add."""
    for col, typ in [
        ("shares_short_prior_month", "BIGINT"),
        ("short_change_mom_pct", "NUMERIC(10, 2)"),
        ("date_short_interest", "DATE"),
        ("date_short_prior", "DATE"),
        ("net_borrowing", "NUMERIC(20, 2)"),
        ("data_source", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE us_short_interest_daily ADD COLUMN IF NOT EXISTS {col} {typ}")
        except Exception:
            pass
    try:
        conn.commit()
    except Exception:
        pass


def _select_targets(conn, penny_only, symbols, refetch_stale_days, limit):
    if symbols:
        return symbols
    where = ["exchange IN ('NASDAQ','NYSE','NYSE_AMEX')", "(is_etf = FALSE OR is_etf IS NULL)"]
    if penny_only:
        where.append("is_penny = TRUE")
    where_sql = " AND ".join(where)
    sql = f"SELECT ticker FROM us_stocks WHERE {where_sql} ORDER BY is_penny DESC, market_cap_usd ASC"
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    cur = conn.execute(sql)
    return [r[0] for r in cur.fetchall() if r[0]]


def sync(targets, delay: float = 0.4) -> tuple[int, int]:
    if not targets:
        return 0, 0
    print(f"[sync_us_short_enhanced] {len(targets)} tickers, delay={delay}s")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    conn = get_stocks_conn()
    _ensure_table(conn)
    fetched = 0
    upserted = 0

    for i, sym in enumerate(targets):
        try:
            d = fetch_short_all(sym)
        except Exception as exc:
            logger.debug("fetch %s: %s", sym, exc)
            d = None
        fetched += 1
        if not d or (not d.get("shares_short") and not d.get("short_pct_float")):
            time.sleep(delay)
            continue
        source = "yf+sa" if (d["has_yfinance"] and d["has_stockanalysis"]) else "yf" if d["has_yfinance"] else "sa"
        try:
            conn.execute(
                """
                INSERT INTO us_short_interest_daily (
                    symbol, as_of_date,
                    short_interest_shares, short_float_pct, days_to_cover,
                    shares_short_prior_month, short_change_mom_pct,
                    date_short_interest, date_short_prior, net_borrowing, data_source,
                    fetched_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, as_of_date) DO UPDATE SET
                    short_interest_shares = excluded.short_interest_shares,
                    short_float_pct = excluded.short_float_pct,
                    days_to_cover = excluded.days_to_cover,
                    shares_short_prior_month = excluded.shares_short_prior_month,
                    short_change_mom_pct = excluded.short_change_mom_pct,
                    date_short_interest = excluded.date_short_interest,
                    date_short_prior = excluded.date_short_prior,
                    net_borrowing = excluded.net_borrowing,
                    data_source = excluded.data_source,
                    fetched_at = excluded.fetched_at
                """,
                (
                    sym, today,
                    d.get("shares_short"),
                    d.get("short_pct_float"),
                    d.get("days_to_cover"),
                    d.get("shares_short_prior"),
                    d.get("short_change_mom_pct"),
                    d.get("date_short_interest"),
                    d.get("date_short_prior"),
                    d.get("net_borrowing"),
                    source,
                    now,
                ),
            )
            upserted += 1
        except Exception as exc:
            logger.debug("upsert %s: %s", sym, exc)
        if (i + 1) % 20 == 0:
            conn.commit()
            print(f"  [{i+1}/{len(targets)}] fetched={fetched} upserted={upserted}")
        time.sleep(delay)
    conn.commit()
    conn.close()
    return fetched, upserted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--penny-only", action="store_true")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--refetch-stale", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.4)
    args = ap.parse_args()

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] if args.symbols else []
    conn = get_stocks_conn()
    try:
        targets = _select_targets(conn, args.penny_only, syms, args.refetch_stale, args.limit)
        print(f"[targets] {len(targets)}")
    finally:
        conn.close()
    if not targets:
        return 0
    fetched, upserted = sync(targets, delay=args.delay)
    print(f"[done] fetched={fetched} upserted={upserted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
