"""us_stocks.sector 가 NULL 인 종목을 SEC SIC code 기반 자동 채움.

페니 sector 100% NULL 문제 해결.
1. sic_code 있으면 sic_to_sector 매핑 적용
2. sic_code 없으면 SEC EDGAR submissions API 에서 가져와 us_stocks 업데이트
3. 그래도 없으면 sector="Other" + sector_full=NULL

사용:
    python scripts/backfill_sector_from_sic.py --penny-only
    python scripts/backfill_sector_from_sic.py --null-sector-only
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

from collectors.sic_to_sector import sic_to_sector  # noqa: E402
from collectors.us_sec_filings import _load_cik_mapping, _CIK_CACHE  # noqa: E402
from server.db.connections import get_stocks_conn  # noqa: E402

logger = logging.getLogger("scripts.backfill_sector_from_sic")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _ensure_columns(conn) -> None:
    """sic_code / sic_description 컬럼 없으면 추가."""
    for col_sql in [
        "ALTER TABLE us_stocks ADD COLUMN IF NOT EXISTS sic_code TEXT",
        "ALTER TABLE us_stocks ADD COLUMN IF NOT EXISTS sic_description TEXT",
        "ALTER TABLE us_stocks ADD COLUMN IF NOT EXISTS sector_full TEXT",
    ]:
        try:
            conn.execute(col_sql)
        except Exception:
            pass
    try:
        conn.commit()
    except Exception:
        pass


def fetch_sic_from_sec(symbol: str) -> tuple[str | None, str | None]:
    """SEC submissions API 에서 SIC code/description 직접 fetch."""
    import requests
    _load_cik_mapping()
    cik = _CIK_CACHE.get(symbol.upper())
    if not cik:
        return None, None
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": "JurinMapBot research@example.com",
                "Accept": "application/json",
                "Host": "data.sec.gov",
            },
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        return str(data.get("sic") or ""), data.get("sicDescription")
    except Exception as exc:
        logger.debug("SEC submissions %s: %s", symbol, exc)
        return None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--penny-only", action="store_true")
    ap.add_argument("--null-sector-only", action="store_true", default=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.12)
    ap.add_argument("--fetch-sec", action="store_true", help="sic_code 없으면 SEC submissions API 호출")
    args = ap.parse_args()

    conn = get_stocks_conn()
    _ensure_columns(conn)
    where_parts = ["exchange IN ('NASDAQ','NYSE','NYSE_AMEX')", "(is_etf = FALSE OR is_etf IS NULL)"]
    if args.penny_only:
        where_parts.append("is_penny = TRUE")
    if args.null_sector_only:
        where_parts.append("(sector IS NULL OR sector = '')")
    sql = f"SELECT ticker, sic_code FROM us_stocks WHERE {' AND '.join(where_parts)} ORDER BY market_cap_usd DESC NULLS LAST"
    if args.limit > 0:
        sql += f" LIMIT {int(args.limit)}"
    cur = conn.execute(sql)
    targets = cur.fetchall()
    print(f"[targets] {len(targets)}")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    matched_from_sic = 0
    matched_from_sec = 0
    unmatched = 0

    for i, r in enumerate(targets):
        ticker = r[0]
        sic = r[1]
        sector, sector_full = (None, None)
        sic_desc = None

        # 1. DB에 sic_code 있으면 매핑 우선
        if sic:
            sector, sector_full = sic_to_sector(sic)
            if sector:
                matched_from_sic += 1

        # 2. 없으면 SEC submissions 에서 가져오기 (옵션)
        if not sector and args.fetch_sec:
            sic_fetched, sic_desc = fetch_sic_from_sec(ticker)
            if sic_fetched:
                sector, sector_full = sic_to_sector(sic_fetched)
                if sector:
                    matched_from_sec += 1
                    sic = sic_fetched
                # sic_code 자체도 DB 업데이트
                try:
                    conn.execute(
                        "UPDATE us_stocks SET sic_code = %s, sic_description = COALESCE(NULLIF(sic_description, ''), %s) WHERE ticker = %s",
                        (sic_fetched, sic_desc, ticker),
                    )
                except Exception:
                    pass
            time.sleep(args.delay)

        if not sector:
            unmatched += 1
            continue

        try:
            conn.execute(
                """
                UPDATE us_stocks SET sector = %s, sector_full = COALESCE(NULLIF(sector_full, ''), %s)
                WHERE ticker = %s
                """,
                (sector, sector_full or sic_desc, ticker),
            )
        except Exception as exc:
            logger.debug("update %s: %s", ticker, exc)

        if (i + 1) % 50 == 0:
            conn.commit()
            print(f"  [{i+1}/{len(targets)}] sic_match={matched_from_sic} sec_fetch={matched_from_sec} unmatched={unmatched}")

    conn.commit()
    conn.close()
    print(f"[done] sic_match={matched_from_sic} sec_fetch={matched_from_sec} unmatched={unmatched}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
