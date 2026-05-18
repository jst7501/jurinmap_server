"""FINRA Reg SHO Daily Short Volume → us_short_volume_daily upsert.

매일 EOD 후 한 번 실행. T+1 가까운 신선도 — 우리 미국 공매도 데이터 중 가장
"실시간"에 가까운 시그널 (당일 거래 중 공매도 비중).

사용:
    python scripts/sync_us_short_volume.py            # 가장 최근 가용 일자
    python scripts/sync_us_short_volume.py --days 5   # 최근 5일치 backfill
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from collectors.us_finra_short_volume import _fetch_text, parse  # type: ignore  # noqa: E402
from server.db.connections import get_stocks_conn  # noqa: E402


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS us_short_volume_daily (
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            short_volume DOUBLE PRECISION,
            short_exempt_volume DOUBLE PRECISION,
            total_volume DOUBLE PRECISION,
            short_volume_ratio DOUBLE PRECISION,
            market TEXT,
            source TEXT DEFAULT 'finra_cnms',
            fetched_at TEXT,
            PRIMARY KEY (symbol, trade_date)
        )
        """
    )
    try:
        conn.commit()
    except Exception:
        pass


def _yyyymmdd_to_iso(s: str) -> str:
    """20260513 → 2026-05-13"""
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def sync_day(date_yyyymmdd: str) -> tuple[int, int]:
    """한 날짜 sync. 반환: (received_rows, upserted_rows)."""
    text = _fetch_text("CNMS", date_yyyymmdd)
    if not text:
        return 0, 0
    rows = parse(text)
    if not rows:
        return 0, 0
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    iso_date = _yyyymmdd_to_iso(date_yyyymmdd)
    conn = get_stocks_conn()
    upserted = 0
    try:
        _ensure_table(conn)
        # 일괄 INSERT
        for r in rows:
            try:
                conn.execute(
                    """
                    INSERT INTO us_short_volume_daily
                        (symbol, trade_date, short_volume, short_exempt_volume,
                         total_volume, short_volume_ratio, market, source, fetched_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (symbol, trade_date) DO UPDATE SET
                        short_volume = excluded.short_volume,
                        short_exempt_volume = excluded.short_exempt_volume,
                        total_volume = excluded.total_volume,
                        short_volume_ratio = excluded.short_volume_ratio,
                        market = excluded.market,
                        source = excluded.source,
                        fetched_at = excluded.fetched_at
                    """,
                    (
                        r["symbol"], iso_date,
                        r["short_volume"], r["short_exempt_volume"],
                        r["total_volume"], r["short_volume_ratio"],
                        r["market"], "finra_cnms", now_iso,
                    ),
                )
                upserted += 1
            except Exception as e:
                print(f"  [warn] {r['symbol']}: {e}", file=sys.stderr)
        conn.commit()
    finally:
        conn.close()
    return len(rows), upserted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1,
                    help="가장 최근 가용 일자에서 몇 일치 backfill (기본 1)")
    ap.add_argument("--lookback", type=int, default=7,
                    help="최근 가용 일자 탐색 범위")
    args = ap.parse_args()

    today_et = datetime.now(timezone.utc) - timedelta(hours=4)
    synced = 0
    for d in range(args.lookback):
        dt = (today_et - timedelta(days=d)).strftime("%Y%m%d")
        text = _fetch_text("CNMS", dt)
        if not text:
            continue
        # 최초 valid 일자 발견 → 거기서 days 만큼 sync
        for sd in range(args.days):
            target = (datetime.strptime(dt, "%Y%m%d") - timedelta(days=sd)).strftime("%Y%m%d")
            received, upserted = sync_day(target)
            iso = _yyyymmdd_to_iso(target)
            if received:
                print(f"  {iso}: received={received:,} upserted={upserted:,}")
                synced += 1
            else:
                print(f"  {iso}: no data")
        break
    if synced == 0:
        print("[sync_us_short_volume] no fresh FINRA data in lookback window", file=sys.stderr)
        return 1
    print(f"[sync_us_short_volume] done — {synced} day(s) synced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
