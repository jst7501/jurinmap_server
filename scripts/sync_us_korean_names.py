"""us_stocks 의 name_ko (한글 회사명) 채우기 — 네이버 ac API.

전략 — 우선순위:
  1. Reddit mention 풀 + 다른 핫 종목 (recent_priority=True) — 빠른 적용 ~수백
  2. 전체 us_stocks 12000+ — 느린 backfill (~30분)

Usage:
  python scripts/sync_us_korean_names.py --priority-only    # 핫 ticker 만 (~500)
  python scripts/sync_us_korean_names.py                    # 전체 (~30분)
  python scripts/sync_us_korean_names.py --refetch-null     # 기존 NULL 만 재시도
  python scripts/sync_us_korean_names.py --delay 0.1        # rate 조정
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

from collectors.us_korean_names import fetch_korean_name  # noqa: E402
from server.db.connections import get_stocks_conn  # noqa: E402

logger = logging.getLogger("scripts.sync_us_korean_names")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _ensure_columns(conn) -> None:
    try:
        conn.execute("ALTER TABLE us_stocks ADD COLUMN IF NOT EXISTS name_ko TEXT")
        conn.execute("ALTER TABLE us_stocks ADD COLUMN IF NOT EXISTS name_ko_updated_at TIMESTAMP")
        conn.commit()
    except Exception as exc:
        logger.debug("ensure_columns: %s", exc)


def _priority_tickers(conn) -> list[str]:
    """Reddit 최근 snapshot 등장 ticker (mention >=2) + 다른 핫 ticker 합쳐서 반환."""
    syms: set[str] = set()

    # 1) Reddit mention 풀
    try:
        cur = conn.execute(
            """
            SELECT DISTINCT symbol FROM us_reddit_mentions_snapshot
            WHERE snapshot_at >= (NOW() - INTERVAL '3 days')
              AND mention_count >= 2
            """
        )
        for row in cur.fetchall():
            syms.add(row[0])
    except Exception as exc:
        logger.warning("priority reddit query failed: %s", exc)

    # 2) us_short_interest_daily 상위 (squeeze 후보)
    try:
        cur = conn.execute(
            """
            SELECT DISTINCT symbol FROM us_short_interest_daily
            WHERE as_of_date >= (CURRENT_DATE - INTERVAL '14 days')::text
            LIMIT 500
            """
        )
        for row in cur.fetchall():
            syms.add(row[0])
    except Exception:
        pass

    # 3) us_threshold_securities_daily (Reg SHO)
    try:
        cur = conn.execute(
            """
            SELECT DISTINCT symbol FROM us_threshold_securities_daily
            WHERE as_of_date >= (CURRENT_DATE - INTERVAL '30 days')::text
            """
        )
        for row in cur.fetchall():
            syms.add(row[0])
    except Exception:
        pass

    # 4) us_short_volume_daily 최근 (큰 종목)
    try:
        cur = conn.execute(
            """
            SELECT DISTINCT symbol FROM us_short_volume_daily
            WHERE trade_date >= (CURRENT_DATE - INTERVAL '7 days')::text
            LIMIT 500
            """
        )
        for row in cur.fetchall():
            syms.add(row[0])
    except Exception:
        pass

    return sorted(syms)


def _all_tickers(conn, only_missing: bool = False) -> list[str]:
    """us_stocks 전체 ticker. only_missing=True 면 name_ko IS NULL 만."""
    where = "WHERE name_ko IS NULL" if only_missing else ""
    cur = conn.execute(f"SELECT ticker FROM us_stocks {where} ORDER BY ticker")
    return [row[0] for row in cur.fetchall() if row[0]]


def sync(targets: list[str], delay: float = 0.15) -> tuple[int, int, int]:
    """Returns: (fetched, matched, errors)"""
    now_iso = datetime.now(timezone.utc).replace(tzinfo=None)
    conn = get_stocks_conn()
    matched = 0
    errors = 0
    fetched = 0
    try:
        _ensure_columns(conn)
        for i, sym in enumerate(targets):
            try:
                kor = fetch_korean_name(sym)
            except Exception:
                errors += 1
                continue
            fetched += 1
            if kor:
                try:
                    conn.execute(
                        """
                        UPDATE us_stocks
                        SET name_ko = %s, name_ko_updated_at = %s
                        WHERE ticker = %s
                        """,
                        (kor, now_iso, sym),
                    )
                    matched += 1
                except Exception as exc:
                    errors += 1
                    logger.debug("update %s: %s", sym, exc)
            if (i + 1) % 100 == 0:
                conn.commit()
                print(f"  [{i+1}/{len(targets)}] matched={matched} errors={errors}")
            import time
            if i < len(targets) - 1 and delay > 0:
                time.sleep(delay)
        conn.commit()
    finally:
        conn.close()
    return fetched, matched, errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--priority-only", action="store_true", help="Reddit mention/short 핫 ticker 만")
    ap.add_argument("--refetch-null", action="store_true", help="기존 name_ko NULL 만 재시도")
    ap.add_argument("--delay", type=float, default=0.15, help="요청 사이 sleep 초 (default 0.15)")
    ap.add_argument("--limit", type=int, default=0, help="N개만 처리 (0=무제한)")
    args = ap.parse_args()

    conn = get_stocks_conn()
    try:
        _ensure_columns(conn)
        if args.priority_only:
            targets = _priority_tickers(conn)
            print(f"[priority targets] {len(targets)} tickers (Reddit + short interest + threshold + short volume)")
        elif args.refetch_null:
            targets = _all_tickers(conn, only_missing=True)
            print(f"[refetch-null] {len(targets)} tickers with name_ko IS NULL")
        else:
            targets = _all_tickers(conn, only_missing=False)
            print(f"[full sync] {len(targets)} tickers")
    finally:
        conn.close()

    if args.limit > 0:
        targets = targets[:args.limit]
        print(f"  (limited to {args.limit})")

    if not targets:
        print("no targets, exit")
        return 0

    fetched, matched, errors = sync(targets, delay=args.delay)
    print(f"[sync_us_korean_names] fetched={fetched} matched={matched} errors={errors}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
