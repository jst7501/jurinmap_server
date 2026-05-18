"""us_stock_quote_cache 백그라운드 refresh worker.

페이지 진입 시 외부 API 호출 X → cron 이 미리 DB 채움.
페니 universe 우선 + stale 우선 정렬.

권장 cron:
  # NY 정규장 (KST 23:30 ~ 06:00) — 매 5분
  */5 23 * * *  python scripts/sync_us_quote_cache.py --penny-only --limit 100 --max-age-min 5
  0,5,10,15,20,25,30,35,40,45,50,55 0,1,2,3,4,5 * * *  python scripts/sync_us_quote_cache.py --penny-only --limit 100

  # 프리/애프터 (KST 18-23, 06-10) — 매 15분
  */15 6-10,18-23 * * *  python scripts/sync_us_quote_cache.py --penny-only --limit 100

  # 장 마감 (그 외) — 매 1시간
  0 * * * *  python scripts/sync_us_quote_cache.py --penny-only --limit 50

Finnhub free 60/min — 페니 ~120 종목 / 5분 → 약 24 req/min < 60 OK
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
from server.services.us_quote_cache import (  # noqa: E402
    ensure_quote_cache_table,
    get_quote_cached,
    QUOTE_CACHE_TABLE,
)

logger = logging.getLogger("scripts.sync_us_quote_cache")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _select_targets(conn, penny_only: bool, max_age_min: int, limit: int) -> list[str]:
    where = ["exchange IN ('NASDAQ','NYSE','NYSE_AMEX')", "(is_etf = FALSE OR is_etf IS NULL)"]
    if penny_only:
        where.append("is_penny = TRUE")
    # derivative 제외
    where.append("position('$' in ticker) = 0")
    where.append("right(ticker, 1) NOT IN ('W','U','R')")
    where.append("length(ticker) <= 5")

    if max_age_min > 0:
        # us_stock_quote_cache 와 LEFT JOIN — N분 이상 stale 또는 미수집
        sql = f"""
        SELECT s.ticker, MAX(q.updated_at) as last_q
        FROM us_stocks s
        LEFT JOIN {QUOTE_CACHE_TABLE} q ON q.symbol = s.ticker
        WHERE {' AND '.join(where)}
        GROUP BY s.ticker, s.market_cap_usd
        HAVING MAX(q.updated_at) IS NULL OR MAX(q.updated_at) < NOW() - INTERVAL '{int(max_age_min)} minutes'
        ORDER BY MAX(q.updated_at) ASC NULLS FIRST, MAX(s.market_cap_usd) DESC NULLS LAST
        """
    else:
        sql = f"""
        SELECT ticker FROM us_stocks
        WHERE {' AND '.join(where)}
        ORDER BY market_cap_usd DESC NULLS LAST
        """
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    cur = conn.execute(sql)
    rows = cur.fetchall()
    return [r[0] for r in rows if r and r[0]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--penny-only", action="store_true", default=True)
    ap.add_argument("--max-age-min", type=int, default=10, help="N분 이상 stale 만 갱신 (0=전체)")
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--rate-delay", type=float, default=1.05, help="Finnhub 60/min 준수")
    ap.add_argument("--force-refresh", action="store_true")
    args = ap.parse_args()

    conn = get_stocks_conn()
    try:
        ensure_quote_cache_table(conn)
        targets = _select_targets(conn, args.penny_only, args.max_age_min, args.limit)
        print(f"[targets] {len(targets)} symbols to refresh (stale > {args.max_age_min}min)")
        if not targets:
            return 0

        ok_count = 0
        miss_count = 0
        finnhub_count = 0
        yf_count = 0
        for i, sym in enumerate(targets):
            try:
                q = get_quote_cached(conn, sym, fresh_seconds=0, force_refresh=args.force_refresh)
                src = q.get("source") or q.get("_cache")
                if q.get("current_price") is not None:
                    ok_count += 1
                    if src == "finnhub":
                        finnhub_count += 1
                    elif src == "yfinance":
                        yf_count += 1
                else:
                    miss_count += 1
            except Exception as exc:
                logger.debug("refresh %s: %s", sym, exc)
                miss_count += 1
            if (i + 1) % 25 == 0:
                print(f"  [{i+1}/{len(targets)}] ok={ok_count} miss={miss_count} finnhub={finnhub_count} yf={yf_count}")
            time.sleep(args.rate_delay)
        print(f"[done] ok={ok_count} miss={miss_count} finnhub={finnhub_count} yf={yf_count}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
