"""SEC EDGAR Fail-to-Deliver (FTD) 데이터 → us_ftd_daily DB sync.

월 단위 반월 (a / b) 파일 1개씩 받아서 종목별 결제실패 행 upsert.
SEC 공개 lag 가 약 2주 이므로 최신 파일이 1-2개월 전일 수 있음.

Usage:
  python scripts/sync_us_ftd.py                              # 최근 6개월 backfill
  python scripts/sync_us_ftd.py --year 2026 --month 4 --half a
  python scripts/sync_us_ftd.py --months-back 12             # 1년치 backfill
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from collectors.us_ftd_sec import (  # noqa: E402
    fetch_half_month,
    get_recent_months,
    parse_zip,
)
from server.db.connections import get_stocks_conn  # noqa: E402


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS us_ftd_daily (
            settlement_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            cusip TEXT,
            fail_quantity BIGINT,
            description TEXT,
            price NUMERIC(12, 4),
            source TEXT DEFAULT 'sec_edgar',
            fetched_at TEXT,
            PRIMARY KEY (settlement_date, symbol)
        )
        """
    )
    # symbol 단위 빈번 조회를 위한 index
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_us_ftd_symbol_date ON us_ftd_daily(symbol, settlement_date DESC)")
    except Exception:
        pass
    try:
        conn.commit()
    except Exception:
        pass


def sync_period(year: int, month: int, half: str) -> tuple[int, int, int]:
    """단일 반월 파일 → upsert.

    Returns: (file_row_count, upserted, errors)
    """
    print(f"[sync_us_ftd] fetching {year}-{month:02d}{half}...")
    try:
        raw = fetch_half_month(year, month, half)
    except Exception as exc:
        print(f"  [skip] {year}-{month:02d}{half}: {exc}", file=sys.stderr)
        return 0, 0, 0

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = get_stocks_conn()
    upserted = 0
    errors = 0
    file_count = 0
    try:
        _ensure_table(conn)
        for r in parse_zip(raw):
            file_count += 1
            sym = r.get("symbol")
            qty = r.get("fail_quantity")
            if not sym or qty is None:
                continue
            try:
                conn.execute(
                    """
                    INSERT INTO us_ftd_daily
                        (settlement_date, symbol, cusip, fail_quantity, description, price, source, fetched_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (settlement_date, symbol) DO UPDATE SET
                        cusip = excluded.cusip,
                        fail_quantity = excluded.fail_quantity,
                        description = excluded.description,
                        price = excluded.price,
                        fetched_at = excluded.fetched_at
                    """,
                    (
                        r["settlement_date"], sym, r.get("cusip"),
                        qty, r.get("description"), r.get("price"),
                        "sec_edgar", now_iso,
                    ),
                )
                upserted += 1
            except Exception as exc:
                errors += 1
                if errors <= 3:
                    print(f"  [warn] {sym} {r.get('settlement_date')}: {exc}", file=sys.stderr)
            # commit batch마다
            if upserted % 5000 == 0 and upserted > 0:
                conn.commit()
        conn.commit()
    finally:
        conn.close()
    print(f"  {year}-{month:02d}{half}: file={file_count} upserted={upserted} errors={errors}")
    return file_count, upserted, errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--month", type=int, default=None)
    ap.add_argument("--half", choices=["a", "b"], default=None)
    ap.add_argument("--months-back", type=int, default=6, help="최근 N개월치 backfill (default 6)")
    args = ap.parse_args()

    # 단일 파일 지정
    if args.year and args.month and args.half:
        sync_period(args.year, args.month, args.half)
        return 0

    # 자동 backfill — 최근 N개월의 a/b 둘 다 시도
    targets = get_recent_months(args.months_back)
    total_up = 0
    for y, m, h in targets:
        _, up, _ = sync_period(y, m, h)
        total_up += up
    print(f"[sync_us_ftd] done total upserts={total_up}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
