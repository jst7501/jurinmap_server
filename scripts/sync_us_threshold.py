"""NYSE Threshold Securities daily sync → us_threshold_securities_daily.

Squeeze score 의 bonus flag 로도 쓰임 (threshold 진입 종목은 강제 buy-in 압력 누적).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from collectors.us_threshold_securities import fetch_threshold_date  # noqa: E402
from server.db.connections import get_stocks_conn  # noqa: E402

# NASDAQ Playwright fetcher (lazy import — playwright 미설치 환경 대비)
try:
    from collectors.us_threshold_nasdaq_playwright import (  # noqa: E402
        fetch_via_playwright as _fetch_nasdaq_threshold,
    )
    _NASDAQ_AVAILABLE = True
except Exception:
    _NASDAQ_AVAILABLE = False


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS us_threshold_securities_daily (
            as_of_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT,
            market TEXT,
            market_category TEXT,
            source TEXT DEFAULT 'nyse',
            fetched_at TEXT,
            PRIMARY KEY (as_of_date, symbol, market)
        )
        """
    )
    try:
        conn.commit()
    except Exception:
        pass


def sync_day(date_iso: str, include_nasdaq: bool = True) -> tuple[int, int]:
    """NYSE + (option) NASDAQ Threshold 둘 다 upsert.

    Returns: (fetched_count, upserted_count)
    """
    rows = fetch_threshold_date(date_iso)

    # NASDAQ (Playwright) — 가용 시 같은 날짜 데이터 합치기
    if include_nasdaq and _NASDAQ_AVAILABLE:
        nasdaq_date = date_iso.replace("-", "")
        try:
            res = _fetch_nasdaq_threshold(nasdaq_date)
            if res.get("rows"):
                # NASDAQ 의 자체 date 가 우선 (요청 date 와 다를 수 있음 — 최신 가용)
                nasdaq_actual_date = res.get("as_of_date", date_iso)
                if nasdaq_actual_date == date_iso:
                    rows = rows + res["rows"]
                    print(f"  [nasdaq] +{res['row_count']} rows for {nasdaq_actual_date}")
                else:
                    print(f"  [nasdaq] skipped — file is {nasdaq_actual_date} not {date_iso}", file=sys.stderr)
        except Exception as exc:
            print(f"  [nasdaq] fetch failed: {exc}", file=sys.stderr)

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = get_stocks_conn()
    upserted = 0
    try:
        _ensure_table(conn)
        for r in rows:
            try:
                src = "nasdaq" if r.get("market") == "nasdaq" else "nyse"
                conn.execute(
                    """
                    INSERT INTO us_threshold_securities_daily
                        (as_of_date, symbol, name, market, market_category, source, fetched_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (as_of_date, symbol, market) DO UPDATE SET
                        name = excluded.name,
                        market_category = excluded.market_category,
                        source = excluded.source,
                        fetched_at = excluded.fetched_at
                    """,
                    (
                        date_iso, r["symbol"], r["name"], r["market"],
                        r["market_category"], src, now_iso,
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
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (기본 어제 ET)")
    ap.add_argument("--days", type=int, default=1, help="최근 N영업일 backfill")
    args = ap.parse_args()

    if args.date:
        fetched, up = sync_day(args.date)
        print(f"  {args.date}: fetched={fetched} upserted={up}")
        return 0

    # 어제부터 N일 backfill (주말 포함, 0 짜리 응답이라도 기록 안 함 — 빈 list는 그날 0개 의미)
    today_et = datetime.now(timezone.utc) - timedelta(hours=4)
    total = 0
    for d in range(args.days):
        date_iso = (today_et - timedelta(days=d)).strftime("%Y-%m-%d")
        fetched, up = sync_day(date_iso)
        print(f"  {date_iso}: fetched={fetched} upserted={up}")
        total += up
    print(f"[sync_us_threshold] done total upserts={total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
