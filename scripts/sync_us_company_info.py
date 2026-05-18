"""us_stocks 의 회사 정보 (summary/industry/employees/CEO/float/...) sync.

Usage:
  python scripts/sync_us_company_info.py --penny-only     # 페니 우선 (118개, 5-10분)
  python scripts/sync_us_company_info.py --limit 500      # 메이저도 일부
  python scripts/sync_us_company_info.py                  # 전체 (느림)
  python scripts/sync_us_company_info.py --refetch-stale 30  # 30일 이상 안 갱신
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

from collectors.us_company_info import batch_fetch_company_info  # noqa: E402
from server.db.connections import get_stocks_conn  # noqa: E402

logger = logging.getLogger("scripts.sync_us_company_info")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


COLUMNS = [
    ("summary", "TEXT"),
    ("industry", "TEXT"),
    ("sector_full", "TEXT"),
    ("employees", "INT"),
    ("website", "TEXT"),
    ("country", "TEXT"),
    ("state", "TEXT"),
    ("city", "TEXT"),
    ("hq_address", "TEXT"),
    ("ceo_name", "TEXT"),
    ("ceo_title", "TEXT"),
    ("officers_json", "TEXT"),
    ("shares_outstanding", "BIGINT"),
    ("float_shares", "BIGINT"),
    ("insider_pct", "NUMERIC(7,4)"),
    ("institutional_pct", "NUMERIC(7,4)"),
    ("avg_volume_10d", "BIGINT"),
    ("avg_volume_3m", "BIGINT"),
    ("fifty_two_week_high", "NUMERIC(12,4)"),
    ("fifty_two_week_low", "NUMERIC(12,4)"),
    ("beta", "NUMERIC(7,3)"),
    ("trailing_pe", "NUMERIC(10,3)"),
    ("forward_pe", "NUMERIC(10,3)"),
    ("price_to_book", "NUMERIC(10,3)"),
    ("dividend_yield_pct", "NUMERIC(7,4)"),
    ("company_info_updated_at", "TIMESTAMP"),
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


def _select_targets(
    conn,
    penny_only: bool = False,
    refetch_stale_days: int = 0,
    limit: int = 0,
) -> list[str]:
    where = []
    if penny_only:
        where.append("is_penny = TRUE")
    where.append("exchange IN ('NASDAQ','NYSE','NYSE_AMEX')")
    where.append("(is_etf = FALSE OR is_etf IS NULL)")
    if refetch_stale_days > 0:
        where.append(
            f"(company_info_updated_at IS NULL OR company_info_updated_at < NOW() - INTERVAL '{int(refetch_stale_days)} days')"
        )
    where_sql = " AND ".join(where) if where else "1=1"
    # 페니 우선 후 시총 작은 순 (페니 universe 안에서 더 deep penny 부터)
    order_sql = "ORDER BY is_penny DESC NULLS LAST, market_cap_usd ASC NULLS LAST"
    sql = f"SELECT ticker FROM us_stocks WHERE {where_sql} {order_sql}"
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    cur = conn.execute(sql)
    return [r[0] for r in cur.fetchall() if r[0]]


def sync(targets: list[str], workers: int = 5, batch_commit: int = 50) -> tuple[int, int]:
    if not targets:
        return 0, 0
    print(f"[sync_us_company_info] {len(targets)} tickers, workers={workers}")

    def _progress(done: int, total: int):
        print(f"  [{done}/{total}] ({done/total*100:.0f}%)")

    results = batch_fetch_company_info(targets, workers=workers, progress_callback=_progress)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    conn = get_stocks_conn()
    updated = 0
    try:
        _ensure_columns(conn)
        for i, (sym, info) in enumerate(results.items()):
            if not info:
                continue
            import json as _json
            officers_json = _json.dumps(info.get("officers") or [], ensure_ascii=False) if info.get("officers") else None
            try:
                conn.execute(
                    """
                    UPDATE us_stocks SET
                        summary = %s,
                        industry = %s,
                        sector_full = %s,
                        employees = %s,
                        website = %s,
                        country = %s,
                        state = %s,
                        city = %s,
                        hq_address = %s,
                        ceo_name = %s,
                        ceo_title = %s,
                        officers_json = %s,
                        shares_outstanding = %s,
                        float_shares = %s,
                        insider_pct = %s,
                        institutional_pct = %s,
                        avg_volume_10d = %s,
                        avg_volume_3m = %s,
                        fifty_two_week_high = %s,
                        fifty_two_week_low = %s,
                        beta = %s,
                        trailing_pe = %s,
                        forward_pe = %s,
                        price_to_book = %s,
                        dividend_yield_pct = %s,
                        company_info_updated_at = %s
                    WHERE ticker = %s
                    """,
                    (
                        info.get("summary"), info.get("industry"), info.get("sector_full"),
                        info.get("employees"), info.get("website"),
                        info.get("country"), info.get("state"), info.get("city"), info.get("hq_address"),
                        info.get("ceo_name"), info.get("ceo_title"), officers_json,
                        info.get("shares_outstanding"), info.get("float_shares"),
                        info.get("insider_pct"), info.get("institutional_pct"),
                        info.get("avg_volume_10d"), info.get("avg_volume_3m"),
                        info.get("fifty_two_week_high"), info.get("fifty_two_week_low"),
                        info.get("beta"),
                        info.get("trailing_pe"), info.get("forward_pe"),
                        info.get("price_to_book"), info.get("dividend_yield_pct"),
                        now, sym,
                    ),
                )
                updated += 1
            except Exception as exc:
                logger.debug("update %s: %s", sym, exc)
            if (i + 1) % batch_commit == 0:
                conn.commit()
        conn.commit()
    finally:
        conn.close()
    return len(results), updated


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--penny-only", action="store_true", help="페니만 (118개)")
    ap.add_argument("--refetch-stale", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=5, help="default 5 (rate limit 보호)")
    args = ap.parse_args()

    conn = get_stocks_conn()
    try:
        _ensure_columns(conn)
        targets = _select_targets(
            conn,
            penny_only=args.penny_only,
            refetch_stale_days=args.refetch_stale,
            limit=args.limit,
        )
        print(f"[targets] {len(targets)} tickers")
    finally:
        conn.close()

    if not targets:
        print("no targets")
        return 0

    fetched, updated = sync(targets, workers=args.workers)
    print(f"[done] fetched={fetched} updated={updated}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
