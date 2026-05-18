"""us_yfinance_snapshot_cache 백그라운드 refresh worker.

페이지 진입 시 yfinance.info(1~5초) 동기 호출 X → cron 이 미리 DB 채움.
페니 universe 우선, stale 우선 정렬.

스냅샷에 발행주식수·float·공매도·기관 보유 비율·재무·holders 포함 — 상세페이지
펀더멘털 위젯이 전부 DB hit 으로 즉시 렌더.

권장 cron (펀더멘털은 거의 안 변함 → 3시간마다):
  20 */3 * * *  python scripts/sync_us_yfinance_cache.py --penny-only --limit 100

yfinance.info 는 무겁고 rate limit 잦음 → --rate-delay 2초 권장.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

from server.db.connections import get_stocks_conn  # noqa: E402
from server.services.us_yfinance_cache import (  # noqa: E402
    ensure_yf_cache_table,
    get_snapshot_cached,
    TABLE,
)

logger = logging.getLogger("scripts.sync_us_yfinance_cache")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _select_targets(conn, penny_only: bool, max_age_hours: int, limit: int) -> list[str]:
    where = ["exchange IN ('NASDAQ','NYSE','NYSE_AMEX')", "(is_etf = FALSE OR is_etf IS NULL)"]
    if penny_only:
        where.append("is_penny = TRUE")
    # derivative(워런트·유닛·권리) 제외
    where.append("position('$' in ticker) = 0")
    where.append("right(ticker, 1) NOT IN ('W','U','R')")
    where.append("length(ticker) <= 5")

    if max_age_hours > 0:
        # us_yfinance_snapshot_cache 와 LEFT JOIN — N시간 이상 stale 또는 미수집
        sql = f"""
        SELECT s.ticker, MAX(c.updated_at) as last_u
        FROM us_stocks s
        LEFT JOIN {TABLE} c ON c.symbol = s.ticker
        WHERE {' AND '.join(where)}
        GROUP BY s.ticker, s.market_cap_usd
        HAVING MAX(c.updated_at) IS NULL OR MAX(c.updated_at) < NOW() - INTERVAL '{int(max_age_hours)} hours'
        ORDER BY MAX(c.updated_at) ASC NULLS FIRST, MAX(s.market_cap_usd) DESC NULLS LAST
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
    ap.add_argument("--max-age-hours", type=int, default=2, help="N시간 이상 stale 만 갱신 (0=전체)")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--rate-delay", type=float, default=2.0, help="yfinance rate limit 회피")
    args = ap.parse_args()

    conn = get_stocks_conn()
    try:
        ensure_yf_cache_table(conn)
        targets = _select_targets(conn, args.penny_only, args.max_age_hours, args.limit)
        print(f"[targets] {len(targets)} symbols to refresh (stale > {args.max_age_hours}h)")
        if not targets:
            return 0

        ok = 0
        empty = 0
        fail = 0
        for i, sym in enumerate(targets):
            try:
                snap = get_snapshot_cached(conn, sym, force_refresh=True)
                if snap:
                    ok += 1
                else:
                    empty += 1
            except Exception as exc:
                logger.debug("refresh %s: %s", sym, exc)
                fail += 1
            if (i + 1) % 20 == 0:
                print(f"  [{i+1}/{len(targets)}] ok={ok} empty={empty} fail={fail}")
            time.sleep(args.rate_delay)
        print(f"[done] ok={ok} empty={empty} fail={fail}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
