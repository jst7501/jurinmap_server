"""Finnhub free tier sync — yfinance 누락 페니 메타 보강.

대상: us_stocks 중 industry/ceo_name/employees/website/sector 중 1개라도 NULL 인 종목.
Finnhub /stock/profile2 호출 (60/min rate limit 준수).

사용:
    export FINNHUB_API_KEY=xxxxx
    python scripts/sync_us_finnhub_meta.py --penny-only
    python scripts/sync_us_finnhub_meta.py --null-only --limit 100
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

from collectors.us_finnhub import get_company_profile, _API_KEY  # noqa: E402
from server.db.connections import get_stocks_conn  # noqa: E402

logger = logging.getLogger("scripts.sync_us_finnhub_meta")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _select_targets(conn, penny_only: bool, null_only: bool, limit: int, symbols: list[str] | None = None) -> list[str]:
    if symbols:
        return [s.upper() for s in symbols if s]
    where = ["exchange IN ('NASDAQ','NYSE','NYSE_AMEX')", "(is_etf = FALSE OR is_etf IS NULL)"]
    if penny_only:
        where.append("is_penny = TRUE")
    if null_only:
        where.append("""(
            industry IS NULL OR industry = '' OR
            website IS NULL OR website = '' OR
            sector IS NULL OR sector = '' OR
            shares_outstanding IS NULL OR shares_outstanding = 0 OR
            employees IS NULL OR employees = 0 OR
            market_cap_usd IS NULL OR market_cap_usd = 0 OR
            last_price IS NULL OR last_price = 0
        )""")
    # derivative 제외 (preferred·warrant·unit·right)
    where.append("position('$' in ticker) = 0")
    where.append("right(ticker, 1) NOT IN ('W','U','R')")
    where.append("length(ticker) <= 5")
    # NULL market_cap 종목 우선 (가장 비어있는 것부터 채움)
    sql = f"SELECT ticker FROM us_stocks WHERE {' AND '.join(where)} ORDER BY market_cap_usd ASC NULLS FIRST"
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    cur = conn.execute(sql)
    return [r[0] for r in cur.fetchall() if r and r[0]]


def main() -> int:
    if not _API_KEY:
        print("ERROR: FINNHUB_API_KEY 환경변수 미설정")
        print("발급: https://finnhub.io/dashboard")
        return 2

    ap = argparse.ArgumentParser()
    ap.add_argument("--penny-only", action="store_true")
    ap.add_argument("--null-only", action="store_true", default=True)
    ap.add_argument("--symbols", default="", help="콤마 구분 — 특정 종목만 sync (WOK,VNET,...)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=1.05, help="rate limit 준수 — 60/min = 1초/콜")
    args = ap.parse_args()

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    conn = get_stocks_conn()
    targets = _select_targets(conn, args.penny_only, args.null_only, args.limit, symbols=syms)
    print(f"[targets] {len(targets)} tickers")
    if not targets:
        return 0

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    updated = 0
    no_data = 0
    failed = 0

    for i, sym in enumerate(targets):
        try:
            p = get_company_profile(sym)
        except Exception as exc:
            logger.debug("%s: %s", sym, exc)
            p = None
            failed += 1
        if not p:
            no_data += 1
            time.sleep(args.delay)
            continue

        try:
            # Finnhub 단위: marketCapitalization 은 $M, shareOutstanding 은 M주
            mc_usd = float(p["market_cap_musd"]) * 1e6 if p.get("market_cap_musd") else None
            shares = int(float(p["shares_outstanding_m"]) * 1e6) if p.get("shares_outstanding_m") else None

            # quote 도 같이 가져와서 last_price 채움 (1 API call 더, 60/min 안 넘음)
            try:
                from collectors.us_finnhub import get_quote
                q = get_quote(sym)
                last_price = q.get("current_price") if q else None
            except Exception:
                last_price = None

            # NULL 0 둘 다 비어있는 것으로 취급 — NULLIF로 0 도 NULL 변환
            conn.execute(
                """
                UPDATE us_stocks SET
                    industry = COALESCE(NULLIF(industry, ''), %s),
                    website = COALESCE(NULLIF(website, ''), %s),
                    phone = COALESCE(NULLIF(phone, ''), %s),
                    country = COALESCE(NULLIF(country, ''), %s),
                    last_price = COALESCE(NULLIF(last_price, 0), %s),
                    market_cap_usd = COALESCE(NULLIF(market_cap_usd, 0), %s),
                    shares_outstanding = COALESCE(NULLIF(shares_outstanding, 0), %s),
                    market_cap_updated_at = %s
                WHERE ticker = %s
                """,
                (
                    p.get("industry"),
                    p.get("weburl"),
                    p.get("phone"),
                    p.get("country"),
                    last_price,
                    mc_usd,
                    shares,
                    now,
                    sym,
                ),
            )
            updated += 1
        except Exception as exc:
            logger.debug("upsert %s: %s", sym, exc)
            failed += 1

        if (i + 1) % 30 == 0:
            try:
                conn.commit()
            except Exception:
                pass
            print(f"  [{i+1}/{len(targets)}] updated={updated} no_data={no_data} failed={failed}")
        time.sleep(args.delay)

    try:
        conn.commit()
    except Exception:
        pass
    conn.close()
    print(f"[done] updated={updated} no_data={no_data} failed={failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
