"""SEC Company Facts sync — us_sec_financials 테이블.

Cash runway / burn rate / 분기별 시계열. 페니 분석 핵심.
"""
from __future__ import annotations

import argparse
import json
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

from collectors.us_sec_facts import fetch_sec_facts  # noqa: E402
from server.db.connections import get_stocks_conn  # noqa: E402

logger = logging.getLogger("scripts.sync_us_sec_facts")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS us_sec_financials (
            symbol TEXT NOT NULL PRIMARY KEY,
            cik TEXT,
            entity_name TEXT,
            revenue_usd NUMERIC(20),
            revenue_end DATE,
            revenue_form TEXT,
            net_income_usd NUMERIC(20),
            net_income_end DATE,
            net_income_form TEXT,
            cash_usd NUMERIC(20),
            cash_end DATE,
            cash_form TEXT,
            op_cash_usd NUMERIC(20),
            op_cash_end DATE,
            op_cash_form TEXT,
            assets_usd NUMERIC(20),
            liabilities_usd NUMERIC(20),
            equity_usd NUMERIC(20),
            shares_outstanding NUMERIC(20),
            burn_monthly_usd NUMERIC(20),
            cash_runway_months NUMERIC(7, 2),
            revenue_series_json TEXT,
            net_income_series_json TEXT,
            cash_series_json TEXT,
            op_cash_series_json TEXT,
            updated_at TIMESTAMP
        )
        """
    )
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sec_fin_runway ON us_sec_financials(cash_runway_months)")
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
    where_sql = " AND ".join(where) if where else "1=1"
    if refetch_stale_days > 0:
        sql = f"""
        SELECT us.ticker FROM us_stocks us
        LEFT JOIN us_sec_financials sf ON sf.symbol = us.ticker
        WHERE {where_sql}
          AND (sf.updated_at IS NULL OR sf.updated_at < NOW() - INTERVAL '{int(refetch_stale_days)} days')
        ORDER BY us.is_penny DESC, us.market_cap_usd ASC
        """
    else:
        sql = f"SELECT ticker FROM us_stocks WHERE {where_sql} ORDER BY is_penny DESC, market_cap_usd ASC"
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    cur = conn.execute(sql)
    return [r[0] for r in cur.fetchall() if r[0]]


def _val(item):
    return item.get("val") if isinstance(item, dict) else None


def _end(item):
    return item.get("end") if isinstance(item, dict) else None


def _form(item):
    return item.get("form") if isinstance(item, dict) else None


def sync(targets, delay: float = 0.12) -> tuple[int, int]:
    if not targets:
        return 0, 0
    print(f"[sync_us_sec_facts] {len(targets)} tickers, delay={delay}s")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    conn = get_stocks_conn()
    _ensure_table(conn)
    fetched = 0
    upserted = 0

    for i, sym in enumerate(targets):
        try:
            f = fetch_sec_facts(sym)
        except Exception as exc:
            logger.debug("fetch %s: %s", sym, exc)
            f = None
        fetched += 1
        if not f:
            time.sleep(delay)
            continue
        l = f["latest"]
        s = f["series"]
        try:
            conn.execute(
                """
                INSERT INTO us_sec_financials (
                    symbol, cik, entity_name,
                    revenue_usd, revenue_end, revenue_form,
                    net_income_usd, net_income_end, net_income_form,
                    cash_usd, cash_end, cash_form,
                    op_cash_usd, op_cash_end, op_cash_form,
                    assets_usd, liabilities_usd, equity_usd, shares_outstanding,
                    burn_monthly_usd, cash_runway_months,
                    revenue_series_json, net_income_series_json,
                    cash_series_json, op_cash_series_json,
                    updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol) DO UPDATE SET
                    cik = excluded.cik,
                    entity_name = excluded.entity_name,
                    revenue_usd = excluded.revenue_usd,
                    revenue_end = excluded.revenue_end,
                    revenue_form = excluded.revenue_form,
                    net_income_usd = excluded.net_income_usd,
                    net_income_end = excluded.net_income_end,
                    net_income_form = excluded.net_income_form,
                    cash_usd = excluded.cash_usd,
                    cash_end = excluded.cash_end,
                    cash_form = excluded.cash_form,
                    op_cash_usd = excluded.op_cash_usd,
                    op_cash_end = excluded.op_cash_end,
                    op_cash_form = excluded.op_cash_form,
                    assets_usd = excluded.assets_usd,
                    liabilities_usd = excluded.liabilities_usd,
                    equity_usd = excluded.equity_usd,
                    shares_outstanding = excluded.shares_outstanding,
                    burn_monthly_usd = excluded.burn_monthly_usd,
                    cash_runway_months = excluded.cash_runway_months,
                    revenue_series_json = excluded.revenue_series_json,
                    net_income_series_json = excluded.net_income_series_json,
                    cash_series_json = excluded.cash_series_json,
                    op_cash_series_json = excluded.op_cash_series_json,
                    updated_at = excluded.updated_at
                """,
                (
                    f["symbol"], f["cik"], f.get("entity_name"),
                    _val(l["revenue"]), _end(l["revenue"]), _form(l["revenue"]),
                    _val(l["net_income"]), _end(l["net_income"]), _form(l["net_income"]),
                    _val(l["cash"]), _end(l["cash"]), _form(l["cash"]),
                    _val(l["op_cash"]), _end(l["op_cash"]), _form(l["op_cash"]),
                    _val(l["assets"]), _val(l["liabilities"]), _val(l["equity"]),
                    _val(l["shares_outstanding"]),
                    f.get("burn_monthly_usd"), f.get("cash_runway_months"),
                    json.dumps(s.get("revenue_q") or [], default=str),
                    json.dumps(s.get("net_income_q") or [], default=str),
                    json.dumps(s.get("cash_q") or [], default=str),
                    json.dumps(s.get("op_cash_q") or [], default=str),
                    now,
                ),
            )
            upserted += 1
        except Exception as exc:
            logger.debug("upsert %s: %s", sym, exc)
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
    ap.add_argument("--delay", type=float, default=0.12)
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
