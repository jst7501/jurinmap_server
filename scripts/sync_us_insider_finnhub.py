"""Finnhub /stock/insider-transactions sync.

OpenInsider 가 페니 ADR 미커버 → Finnhub 가 보강.
us_insider_trades 테이블에 동일 schema 로 upsert (source 컬럼 추가).

사용:
    python scripts/sync_us_insider_finnhub.py --penny-only --null-only
"""
from __future__ import annotations

import argparse
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

from collectors.us_finnhub import get_insider_transactions, _get_api_key  # noqa: E402
from server.db.connections import get_stocks_conn  # noqa: E402

logger = logging.getLogger("scripts.sync_us_insider_finnhub")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _select_targets(conn, penny_only: bool, null_only: bool, limit: int) -> list[str]:
    where = ["exchange IN ('NASDAQ','NYSE','NYSE_AMEX')", "(is_etf = FALSE OR is_etf IS NULL)", "length(ticker) <= 5"]
    if penny_only:
        where.append("is_penny = TRUE")
    if null_only:
        # us_insider_trades 에 데이터 없는 종목만
        where.append("""ticker NOT IN (
            SELECT DISTINCT symbol FROM us_insider_trades
            WHERE trade_date > CURRENT_DATE - INTERVAL '365 days'
        )""")
    sql = f"SELECT ticker FROM us_stocks WHERE {' AND '.join(where)} ORDER BY market_cap_usd DESC NULLS LAST"
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    cur = conn.execute(sql)
    return [r[0] for r in cur.fetchall() if r and r[0]]


def _normalize_trade_type(code: str) -> str:
    """Finnhub transactionCode → us_insider_trades.trade_type ('P', 'S', 'A', 'D')."""
    c = (code or "").strip().upper()
    if c in ("P", "P-PURCHASE", "BUY"):
        return "P"
    if c in ("S", "S-SALE", "SELL"):
        return "S"
    if c.startswith("A") or c == "ACQUISITION":
        return "A"
    if c.startswith("D") or c == "DISPOSITION":
        return "D"
    return c[:1] if c else ""


def main() -> int:
    if not _get_api_key():
        print("ERROR: FINNHUB_API_KEY 미설정")
        return 2

    ap = argparse.ArgumentParser()
    ap.add_argument("--penny-only", action="store_true", default=True)
    ap.add_argument("--null-only", action="store_true", default=True)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = get_stocks_conn()
    targets = _select_targets(conn, args.penny_only, args.null_only, args.limit)
    print(f"[targets] {len(targets)} tickers (OpenInsider 미커버 페니)")
    if not targets:
        return 0

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    total_inserted = 0
    fetched = 0
    no_data = 0

    for i, sym in enumerate(targets):
        try:
            trades = get_insider_transactions(sym, limit=50)
        except Exception as exc:
            logger.debug("fetch %s: %s", sym, exc)
            trades = []
        fetched += 1
        if not trades:
            no_data += 1
            continue
        inserted = 0
        for t in trades:
            try:
                trade_date_str = t.get("transactionDate") or t.get("filingDate")
                if not trade_date_str or not t.get("name"):
                    continue
                trade_date = datetime.strptime(trade_date_str[:10], "%Y-%m-%d").date()
                filing_date = datetime.strptime(t.get("filingDate", trade_date_str)[:10], "%Y-%m-%d").date()
                share = int(t.get("share") or 0)
                change = int(t.get("change") or 0)
                price = float(t.get("transactionPrice") or 0)
                value = abs(change) * price if change and price else 0
                trade_type = _normalize_trade_type(t.get("transactionCode"))

                conn.execute(
                    """
                    INSERT INTO us_insider_trades (
                        symbol, filing_date, trade_date, insider_name, title,
                        trade_type, trade_type_raw, price, qty, owned_after, delta_own_pct, value, fetched_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        sym, filing_date, trade_date,
                        t.get("name"), None,  # Finnhub title 미제공
                        trade_type, t.get("transactionCode") or "",
                        price, abs(change), share,
                        None, value, now,
                    ),
                )
                inserted += 1
            except Exception as exc:
                logger.debug("upsert %s: %s", sym, exc)
        total_inserted += inserted
        if (i + 1) % 20 == 0:
            try:
                conn.commit()
            except Exception:
                pass
            print(f"  [{i+1}/{len(targets)}] fetched={fetched} no_data={no_data} inserted={total_inserted}")
    try:
        conn.commit()
    except Exception:
        pass
    conn.close()
    print(f"[done] fetched={fetched} no_data={no_data} inserted={total_inserted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
