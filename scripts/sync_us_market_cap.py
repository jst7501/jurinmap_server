"""us_stocks 의 market_cap + last_price + is_penny 채우기.

페니 정의: market_cap_usd < $100M.
Universe: NASDAQ + NYSE + NYSE_AMEX, 거래정지/테스트 종목 제외, ETF 제외 (페니 ETF 미관심).

yfinance fast_info 사용 — Ticker.info 의 10배 빠른 cache endpoint.
ThreadPoolExecutor(workers=10) → 8000 종목 ~10분 예상.

Usage:
  python scripts/sync_us_market_cap.py                       # 전체 (NASDAQ+NYSE+AMEX, non-ETF)
  python scripts/sync_us_market_cap.py --refetch-stale 7     # 7일 이상 안 갱신된 것만
  python scripts/sync_us_market_cap.py --penny-only          # 기존 is_penny=true 만 재갱신
  python scripts/sync_us_market_cap.py --workers 5           # rate limit 보호
  python scripts/sync_us_market_cap.py --limit 500           # 테스트용
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

from collectors.us_market_cap import batch_fetch, PENNY_CAP_THRESHOLD_USD  # noqa: E402
from server.db.connections import get_stocks_conn  # noqa: E402

logger = logging.getLogger("scripts.sync_us_market_cap")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _ensure_columns(conn) -> None:
    for stmt in (
        "ALTER TABLE us_stocks ADD COLUMN IF NOT EXISTS market_cap_usd NUMERIC",
        "ALTER TABLE us_stocks ADD COLUMN IF NOT EXISTS last_price NUMERIC",
        "ALTER TABLE us_stocks ADD COLUMN IF NOT EXISTS is_penny BOOLEAN DEFAULT FALSE",
        "ALTER TABLE us_stocks ADD COLUMN IF NOT EXISTS market_cap_updated_at TIMESTAMP",
    ):
        try:
            conn.execute(stmt)
        except Exception as exc:
            logger.debug("alter: %s", exc)
    # is_penny 인덱스 — 페니 필터 쿼리 자주 호출
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_us_stocks_is_penny ON us_stocks(is_penny) WHERE is_penny = TRUE")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_us_stocks_market_cap ON us_stocks(market_cap_usd)")
    except Exception:
        pass
    try:
        conn.commit()
    except Exception:
        pass


def _select_targets(
    conn,
    exchanges: list[str],
    refetch_stale_days: int = 0,
    penny_only: bool = False,
    null_only: bool = False,
    skip_derivatives: bool = True,
    limit: int = 0,
) -> list[str]:
    """sync 대상 ticker 선정.

    null_only=True 면 market_cap/last_price NULL 인 종목만 (sync 한 번 실패한 종목 재시도).
    skip_derivatives=True 면 warrant(끝W)/unit(끝U)/right(끝R)/preferred($포함) 제외.
    """
    where_parts = []
    params: list = []

    # 거래소 필터
    if exchanges:
        placeholders = ",".join(["?"] * len(exchanges))
        where_parts.append(f"exchange IN ({placeholders})")
        params.extend(exchanges)

    # ETF 제외 (페니 ETF 미관심)
    where_parts.append("(is_etf = FALSE OR is_etf IS NULL)")

    # warrant·unit·right·preferred 제외 (db_compat % escape 회피, position/right 사용)
    if skip_derivatives:
        where_parts.append("position('$' in ticker) = 0")    # preferred: VNO$M
        where_parts.append("right(ticker, 1) NOT IN ('W','U','R')")  # warrant·unit·right 끝글자
        where_parts.append("length(ticker) <= 5")             # common 은 보통 5자 이하

    # null_only: market_cap/last_price NULL 인 종목만 재시도
    if null_only:
        where_parts.append("(market_cap_usd IS NULL OR market_cap_usd = 0 OR last_price IS NULL OR last_price = 0)")
    # penny_only: 이미 is_penny=true 인 것만 (재갱신)
    elif penny_only:
        where_parts.append("is_penny = TRUE")
    elif refetch_stale_days > 0:
        # N일 이상 안 갱신된 것만
        where_parts.append(
            f"(market_cap_updated_at IS NULL OR market_cap_updated_at < NOW() - INTERVAL '{int(refetch_stale_days)} days')"
        )

    where = " AND ".join(where_parts) if where_parts else "1=1"
    sql = f"SELECT ticker FROM us_stocks WHERE {where} ORDER BY ticker"
    if limit > 0:
        sql += f" LIMIT {int(limit)}"

    cur = conn.execute(sql, params)
    return [row[0] for row in cur.fetchall() if row[0]]


def sync(targets: list[str], workers: int = 10, batch_commit: int = 200) -> tuple[int, int, int]:
    """Returns: (fetched, updated, penny_count)"""
    print(f"[sync_us_market_cap] {len(targets)} tickers, workers={workers}")
    if not targets:
        return 0, 0, 0

    def _progress(done: int, total: int):
        print(f"  [{done}/{total}] ({done/total*100:.0f}%)")

    results = batch_fetch(targets, workers=workers, progress_callback=_progress)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    conn = get_stocks_conn()
    updated = 0
    penny_count = 0
    try:
        _ensure_columns(conn)
        for i, (sym, info) in enumerate(results.items()):
            mc = info.get("market_cap_usd")
            price = info.get("last_price")
            is_penny = bool(mc is not None and mc < PENNY_CAP_THRESHOLD_USD)
            if is_penny:
                penny_count += 1
            try:
                conn.execute(
                    """
                    UPDATE us_stocks
                    SET market_cap_usd = %s, last_price = %s, is_penny = %s,
                        market_cap_updated_at = %s
                    WHERE ticker = %s
                    """,
                    (mc, price, is_penny, now, sym),
                )
                updated += 1
            except Exception as exc:
                logger.debug("update %s: %s", sym, exc)
            if (i + 1) % batch_commit == 0:
                conn.commit()
        conn.commit()
    finally:
        conn.close()

    return len(results), updated, penny_count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exchanges", default="NASDAQ,NYSE,NYSE_AMEX",
                    help="콤마 구분 거래소 list (default: NASDAQ,NYSE,NYSE_AMEX — 토스 거래 가능)")
    ap.add_argument("--refetch-stale", type=int, default=0, help="N일 이상 안 갱신된 것만 재시도")
    ap.add_argument("--penny-only", action="store_true", help="기존 is_penny=TRUE 만 재갱신")
    ap.add_argument("--null-only", action="store_true", help="market_cap/last_price NULL 인 종목만 재시도 (WOK 같은 sync 실패 종목)")
    ap.add_argument("--include-derivatives", action="store_true", help="warrant·unit·right·preferred 포함 (default: 제외)")
    ap.add_argument("--workers", type=int, default=10, help="동시 worker 수 (default 10)")
    ap.add_argument("--limit", type=int, default=0, help="N개만 처리 (테스트용)")
    args = ap.parse_args()

    exchanges = [e.strip() for e in args.exchanges.split(",") if e.strip()]
    conn = get_stocks_conn()
    try:
        _ensure_columns(conn)
        targets = _select_targets(
            conn, exchanges,
            refetch_stale_days=args.refetch_stale,
            penny_only=args.penny_only,
            null_only=args.null_only,
            skip_derivatives=not args.include_derivatives,
            limit=args.limit,
        )
        print(f"[targets] {len(targets)} tickers in {exchanges} (ETF{', derivatives' if not args.include_derivatives else ''} excluded)")
    finally:
        conn.close()

    if not targets:
        print("no targets")
        return 0

    fetched, updated, penny_count = sync(targets, workers=args.workers)
    print()
    print(f"[done] fetched={fetched} updated={updated} penny={penny_count} (cap < ${PENNY_CAP_THRESHOLD_USD/1e6:.0f}M)")

    # 검증: 페니 분포
    conn = get_stocks_conn()
    try:
        cur = conn.execute("""
            SELECT exchange, COUNT(*) FROM us_stocks
            WHERE is_penny = TRUE AND (is_etf = FALSE OR is_etf IS NULL)
            GROUP BY exchange ORDER BY 2 DESC
        """)
        print()
        print("페니 분포 (is_penny=TRUE, non-ETF):")
        for r in cur.fetchall():
            print(f"  {r[0]:14}  {r[1]:>5}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
