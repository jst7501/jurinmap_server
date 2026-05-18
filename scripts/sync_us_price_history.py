"""us_price_history_cache 사전 채우기 — 페니 134 1년 일봉.

USPumpDumpCard / USHaltPatternCard / US52WeekChart 등이 공용 사용.
페이지 진입 시 yfinance 호출 부담 0 — cron 으로 매일 1회 갱신.

사용:
    python scripts/sync_us_price_history.py --penny-only --period 1y
    python scripts/sync_us_price_history.py --refetch-stale-hours 24
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

from server.db.connections import get_stocks_conn  # noqa: E402
from server.services.us_price_history_cache import (  # noqa: E402
    ensure_table, fetch_yfinance_history, upsert_db, TABLE,
)

logger = logging.getLogger("scripts.sync_us_price_history")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _select_targets(conn, penny_only: bool, refetch_stale_hours: int, limit: int) -> list[str]:
    where = ["exchange IN ('NASDAQ','NYSE','NYSE_AMEX')", "(is_etf = FALSE OR is_etf IS NULL)", "length(ticker) <= 5"]
    if penny_only:
        where.append("is_penny = TRUE")
    where_sql = " AND ".join(where)
    if refetch_stale_hours > 0:
        sql = f"""
        SELECT us.ticker, MAX(phc.updated_at) AS last
        FROM us_stocks us
        LEFT JOIN {TABLE} phc ON phc.symbol = us.ticker AND phc.period = '1y'
        WHERE {where_sql}
        GROUP BY us.ticker, us.market_cap_usd
        HAVING MAX(phc.updated_at) IS NULL OR MAX(phc.updated_at) < NOW() - INTERVAL '{int(refetch_stale_hours)} hours'
        ORDER BY MAX(phc.updated_at) ASC NULLS FIRST, MAX(us.market_cap_usd) DESC NULLS LAST
        """
    else:
        sql = f"""
        SELECT ticker FROM us_stocks WHERE {where_sql}
        ORDER BY market_cap_usd DESC NULLS LAST
        """
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    cur = conn.execute(sql)
    return [r[0] for r in cur.fetchall() if r and r[0]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--penny-only", action="store_true", default=True)
    ap.add_argument("--period", default="1y", help="1mo / 3mo / 6mo / 1y / 2y")
    ap.add_argument("--refetch-stale-hours", type=int, default=24)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.5, help="yfinance rate limit 회피")
    args = ap.parse_args()

    conn = get_stocks_conn()
    ensure_table(conn)
    targets = _select_targets(conn, args.penny_only, args.refetch_stale_hours, args.limit)
    print(f"[targets] {len(targets)} tickers, period={args.period}")
    if not targets:
        return 0

    fetched = 0
    no_data = 0
    upserted = 0
    for i, sym in enumerate(targets):
        try:
            ohlcv = fetch_yfinance_history(sym, period=args.period)
        except Exception as exc:
            logger.debug("history %s: %s", sym, exc)
            ohlcv = None
        fetched += 1
        if not ohlcv or len(ohlcv) < 5:
            no_data += 1
        else:
            try:
                upsert_db(conn, sym, args.period, ohlcv)
                upserted += 1
            except Exception as exc:
                logger.debug("upsert %s: %s", sym, exc)
        if (i + 1) % 20 == 0:
            print(f"  [{i+1}/{len(targets)}] fetched={fetched} upserted={upserted} no_data={no_data}")
        time.sleep(args.delay)

    conn.close()
    print(f"[done] fetched={fetched} upserted={upserted} no_data={no_data}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
