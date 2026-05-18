"""미국 전체 ticker 마스터 DB 동기화 — NASDAQ Trader 공식 directory.

us_stocks 테이블 보강. 매일 1회 실행 권장 (NASDAQ Trader 가 daily 갱신).

Usage:
  python scripts/sync_us_stocks_master.py            # 전체 upsert
  python scripts/sync_us_stocks_master.py --dry-run  # fetch 만, DB 미기록
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

from collectors.us_ticker_master import fetch_all_us_tickers  # noqa: E402
from server.db.connections import get_stocks_conn  # noqa: E402

logger = logging.getLogger("scripts.sync_us_stocks_master")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _ensure_columns(conn) -> None:
    """us_stocks 가 이미 있을 텐데 신규 컬럼만 추가."""
    for stmt in (
        "ALTER TABLE us_stocks ADD COLUMN IF NOT EXISTS market_category TEXT",
        "ALTER TABLE us_stocks ADD COLUMN IF NOT EXISTS is_etf BOOLEAN DEFAULT FALSE",
        "ALTER TABLE us_stocks ADD COLUMN IF NOT EXISTS source TEXT",
    ):
        try:
            conn.execute(stmt)
        except Exception as exc:
            logger.debug("alter table noop: %s", exc)
    try:
        conn.commit()
    except Exception:
        pass


def sync(dry_run: bool = False) -> tuple[int, int, int]:
    """Returns: (fetched, upserted, errors)"""
    rows = fetch_all_us_tickers()
    print(f"[sync_us_stocks_master] fetched {len(rows)} tickers")

    if dry_run:
        return len(rows), 0, 0

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = get_stocks_conn()
    upserted = 0
    errors = 0
    try:
        _ensure_columns(conn)
        for r in rows:
            try:
                # us_stocks PK 는 ticker (이미 존재). name/exchange 갱신, sector/industry/market_cap 은
                # 기존 값 보존 (yfinance 같은 다른 소스가 채울 수 있음 — COALESCE 안 함, 단순 갱신).
                conn.execute(
                    """
                    INSERT INTO us_stocks (ticker, name, exchange, market_category, is_etf, source, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (ticker) DO UPDATE SET
                        name = excluded.name,
                        exchange = excluded.exchange,
                        market_category = excluded.market_category,
                        is_etf = excluded.is_etf,
                        source = excluded.source,
                        updated_at = excluded.updated_at
                    """,
                    (
                        r["ticker"],
                        r["name"][:200] if r["name"] else None,
                        r["exchange"],
                        r.get("market_category"),
                        r.get("is_etf", False),
                        "nasdaq_trader",
                        now_iso,
                    ),
                )
                upserted += 1
            except Exception as exc:
                errors += 1
                if errors <= 3:
                    print(f"  [warn] {r['ticker']}: {exc}", file=sys.stderr)
            if upserted % 2000 == 0 and upserted > 0:
                conn.commit()
        conn.commit()
    finally:
        conn.close()

    print(f"[sync_us_stocks_master] upserted={upserted} errors={errors}")
    return len(rows), upserted, errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    fetched, upserted, errors = sync(dry_run=args.dry_run)
    return 0 if errors < (fetched // 10) else 1


if __name__ == "__main__":
    sys.exit(main())
