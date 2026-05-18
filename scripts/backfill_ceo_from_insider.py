"""us_insider_trades.title 에 'CEO' / 'Chief Executive' 포함된 insider 를
us_stocks.ceo_name 으로 backfill.

페니 50% CEO NULL — OpenInsider (Form 4) 데이터에서 자동 추출.

각 종목별 CEO 후보 중 가장 최근 거래 insider 선택.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

from server.db.connections import get_stocks_conn  # noqa: E402

logger = logging.getLogger("scripts.backfill_ceo_from_insider")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--penny-only", action="store_true", default=True)
    ap.add_argument("--overwrite", action="store_true", help="기존 ceo_name 덮어쓰기 (default: NULL만 채움)")
    args = ap.parse_args()

    conn = get_stocks_conn()

    # 1. CEO 후보 추출 — title 에 'CEO' 또는 'Chief Executive' 포함
    # 각 종목 최신 거래 insider 1명 선택
    print("Step 1: us_insider_trades 에서 CEO 후보 추출")
    cur = conn.execute("""
    WITH ceo_candidates AS (
      SELECT symbol, insider_name, MAX(title) as title, MAX(trade_date) as latest_trade, COUNT(*) as trade_cnt
      FROM us_insider_trades
      WHERE (title ILIKE %s OR title ILIKE %s)
        AND insider_name IS NOT NULL AND insider_name != ''
      GROUP BY symbol, insider_name
    ),
    ranked AS (
      SELECT symbol, insider_name, title, latest_trade,
             ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY latest_trade DESC, trade_cnt DESC) as rn
      FROM ceo_candidates
    )
    SELECT symbol, insider_name, title, latest_trade FROM ranked WHERE rn = 1
    """, ("%CEO%", "%Chief Executive%"))
    candidates = cur.fetchall()
    print(f"  CEO 후보 추출: {len(candidates)}종목")

    # 2. us_stocks UPDATE (페니 우선, NULL 만 또는 덮어쓰기)
    print("\nStep 2: us_stocks 업데이트")
    updated = 0
    for r in candidates:
        sym, insider, title, latest = r[0], r[1], r[2], r[3]
        # 페니 + (NULL 만 또는 overwrite) 조건
        if args.overwrite:
            where_extra = ""
        else:
            where_extra = " AND (ceo_name IS NULL OR ceo_name = '')"
        if args.penny_only:
            where_extra += " AND is_penny = TRUE"
        try:
            n = conn.execute(
                f"""
                UPDATE us_stocks SET
                    ceo_name = %s,
                    ceo_title = COALESCE(NULLIF(ceo_title, ''), %s)
                WHERE ticker = %s {where_extra}
                """,
                (insider, title or 'Chief Executive Officer', sym),
            )
            updated += 1
        except Exception as exc:
            logger.debug("update %s: %s", sym, exc)

    conn.commit()
    conn.close()
    print(f"\n[done] candidates={len(candidates)} update_attempts={updated}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
