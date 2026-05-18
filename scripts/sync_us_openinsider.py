"""OpenInsider 내부자 거래 sync — us_insider_trades 테이블 upsert.

페니 universe 우선. 일 1회 권장.

Usage:
  python scripts/sync_us_openinsider.py --penny-only
  python scripts/sync_us_openinsider.py --symbols NVDA,TSLA
  python scripts/sync_us_openinsider.py --refetch-stale 1
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

from collectors.us_openinsider import fetch_insider_trades, summarize  # noqa: E402
from server.db.connections import get_stocks_conn  # noqa: E402

logger = logging.getLogger("scripts.sync_us_openinsider")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS us_insider_trades (
            symbol TEXT NOT NULL,
            filing_date DATE NOT NULL,
            trade_date DATE,
            insider_name TEXT NOT NULL,
            title TEXT,
            trade_type TEXT,
            trade_type_raw TEXT,
            price NUMERIC(12, 4),
            qty NUMERIC(18, 2),
            owned_after NUMERIC(18, 2),
            delta_own_pct NUMERIC(10, 2),
            value NUMERIC(20, 2),
            fetched_at TIMESTAMP,
            PRIMARY KEY (symbol, filing_date, insider_name, trade_type, price, qty)
        )
        """
    )
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_insider_symbol_date ON us_insider_trades(symbol, trade_date DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_insider_date ON us_insider_trades(trade_date DESC)")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE us_stocks ADD COLUMN IF NOT EXISTS insider_updated_at TIMESTAMP")
    except Exception:
        pass
    try:
        conn.commit()
    except Exception:
        pass


def _select_targets(conn, penny_only, symbols, refetch_stale_days, limit):
    if symbols:
        return symbols
    where = []
    if penny_only:
        where.append("is_penny = TRUE")
    where.append("exchange IN ('NASDAQ','NYSE','NYSE_AMEX')")
    where.append("(is_etf = FALSE OR is_etf IS NULL)")
    if refetch_stale_days > 0:
        where.append(
            f"(insider_updated_at IS NULL OR insider_updated_at < NOW() - INTERVAL '{int(refetch_stale_days)} days')"
        )
    where_sql = " AND ".join(where) if where else "1=1"
    sql = f"SELECT ticker FROM us_stocks WHERE {where_sql} ORDER BY is_penny DESC NULLS LAST, market_cap_usd ASC NULLS LAST"
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    cur = conn.execute(sql)
    return [r[0] for r in cur.fetchall() if r[0]]


def sync(targets, delay: float = 0.3) -> tuple[int, int]:
    if not targets:
        return 0, 0
    print(f"[sync_us_openinsider] {len(targets)} tickers, delay={delay}s")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    conn = get_stocks_conn()
    _ensure_table(conn)
    fetched = 0
    upserted = 0

    for i, sym in enumerate(targets):
        try:
            trades = fetch_insider_trades(sym, days_back=730, limit=100)
        except Exception as exc:
            logger.debug("fetch %s: %s", sym, exc)
            trades = []
        fetched += len(trades)
        for t in trades:
            try:
                conn.execute(
                    """
                    INSERT INTO us_insider_trades
                        (symbol, filing_date, trade_date, insider_name, title,
                         trade_type, trade_type_raw, price, qty, owned_after, delta_own_pct, value, fetched_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (symbol, filing_date, insider_name, trade_type, price, qty) DO UPDATE SET
                        title = excluded.title,
                        trade_type_raw = excluded.trade_type_raw,
                        owned_after = excluded.owned_after,
                        delta_own_pct = excluded.delta_own_pct,
                        value = excluded.value,
                        fetched_at = excluded.fetched_at
                    """,
                    (
                        t["symbol"],
                        t.get("filing_date") or t.get("trade_date") or "1900-01-01",
                        t.get("trade_date"),
                        t.get("insider_name") or "?",
                        t.get("title"),
                        t.get("trade_type") or "?",
                        t.get("trade_type_raw"),
                        t.get("price"),
                        t.get("qty"),
                        t.get("owned_after"),
                        t.get("delta_own_pct"),
                        t.get("value"),
                        now,
                    ),
                )
                upserted += 1
            except Exception as exc:
                logger.debug("upsert %s: %s", sym, exc)
        try:
            conn.execute("UPDATE us_stocks SET insider_updated_at=%s WHERE ticker=%s", (now, sym))
        except Exception:
            pass
        if (i + 1) % 25 == 0:
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
    ap.add_argument("--delay", type=float, default=0.3)
    args = ap.parse_args()

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] if args.symbols else []
    conn = get_stocks_conn()
    try:
        _ensure_table(conn)
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
