"""SEC EDGAR submissions sync — us_sec_filings 테이블.

페니 단타 핵심: dilution filing (424B5/S-1/S-3) 누적 추적 + 8-K AI 요약 대상 큐.
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

from collectors.us_sec_filings import fetch_sec_submissions  # noqa: E402
from server.db.connections import get_stocks_conn  # noqa: E402

logger = logging.getLogger("scripts.sync_us_sec_filings")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS us_sec_filings (
            symbol TEXT NOT NULL,
            accession TEXT NOT NULL,
            cik TEXT,
            form TEXT,
            filing_date DATE,
            report_date DATE,
            primary_doc TEXT,
            primary_doc_desc TEXT,
            items TEXT,
            size_bytes BIGINT,
            doc_url TEXT,
            is_dilution BOOLEAN DEFAULT FALSE,
            is_summary_target BOOLEAN DEFAULT FALSE,
            subtype TEXT,
            created_at TIMESTAMP,
            PRIMARY KEY (symbol, accession)
        )
        """
    )
    # 기존 테이블에 subtype 컬럼 없으면 추가
    try:
        conn.execute("ALTER TABLE us_sec_filings ADD COLUMN IF NOT EXISTS subtype TEXT")
    except Exception:
        pass
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_sec_filings_symbol_date ON us_sec_filings(symbol, filing_date DESC)",
        "CREATE INDEX IF NOT EXISTS idx_sec_filings_form ON us_sec_filings(form, filing_date DESC)",
        "CREATE INDEX IF NOT EXISTS idx_sec_filings_dilution ON us_sec_filings(is_dilution, filing_date DESC) WHERE is_dilution = TRUE",
        "CREATE INDEX IF NOT EXISTS idx_sec_filings_summary ON us_sec_filings(is_summary_target, filing_date DESC) WHERE is_summary_target = TRUE",
        "CREATE INDEX IF NOT EXISTS idx_sec_filings_subtype ON us_sec_filings(subtype, filing_date DESC) WHERE subtype IS NOT NULL",
    ]:
        try:
            conn.execute(idx)
        except Exception:
            pass
    try:
        conn.commit()
    except Exception:
        pass


def _select_targets(conn, penny_only, symbols, refetch_stale_hours, limit):
    if symbols:
        return symbols
    where = ["exchange IN ('NASDAQ','NYSE','NYSE_AMEX')", "(is_etf = FALSE OR is_etf IS NULL)"]
    if penny_only:
        where.append("is_penny = TRUE")
    where_sql = " AND ".join(where)
    if refetch_stale_hours > 0:
        sql = f"""
        SELECT us.ticker, MAX(sf.created_at) AS last_sync FROM us_stocks us
        LEFT JOIN us_sec_filings sf ON sf.symbol = us.ticker
        WHERE {where_sql}
        GROUP BY us.ticker, us.is_penny, us.market_cap_usd
        HAVING MAX(sf.created_at) IS NULL OR MAX(sf.created_at) < NOW() - INTERVAL '{int(refetch_stale_hours)} hours'
        ORDER BY us.is_penny DESC, us.market_cap_usd ASC NULLS LAST
        """
    else:
        sql = f"""
        SELECT ticker FROM us_stocks
        WHERE {where_sql}
        ORDER BY is_penny DESC, market_cap_usd ASC NULLS LAST
        """
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    cur = conn.execute(sql)
    rows = cur.fetchall()
    return [r[0] for r in rows if r and r[0]]


def sync(targets, delay: float = 0.12, max_rows: int = 200) -> tuple[int, int, int]:
    if not targets:
        return 0, 0, 0
    print(f"[sync_us_sec_filings] {len(targets)} tickers, delay={delay}s, max_rows={max_rows}")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    conn = get_stocks_conn()
    _ensure_table(conn)
    fetched = 0
    upserted_total = 0
    skipped = 0

    for i, sym in enumerate(targets):
        try:
            res = fetch_sec_submissions(sym, max_rows=max_rows)
        except Exception as exc:
            logger.debug("fetch %s: %s", sym, exc)
            res = None
        fetched += 1
        if not res:
            skipped += 1
            time.sleep(delay)
            continue
        cik = res.get("cik")
        rows = res.get("filings") or []
        if not rows:
            time.sleep(delay)
            continue
        upserted_here = 0
        for r in rows:
            try:
                conn.execute(
                    """
                    INSERT INTO us_sec_filings (
                        symbol, accession, cik, form, filing_date, report_date,
                        primary_doc, primary_doc_desc, items, size_bytes,
                        doc_url, is_dilution, is_summary_target, subtype, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (symbol, accession) DO UPDATE SET
                        form = EXCLUDED.form,
                        filing_date = EXCLUDED.filing_date,
                        report_date = EXCLUDED.report_date,
                        primary_doc = EXCLUDED.primary_doc,
                        primary_doc_desc = EXCLUDED.primary_doc_desc,
                        items = EXCLUDED.items,
                        size_bytes = EXCLUDED.size_bytes,
                        doc_url = EXCLUDED.doc_url,
                        is_dilution = EXCLUDED.is_dilution,
                        is_summary_target = EXCLUDED.is_summary_target,
                        subtype = EXCLUDED.subtype
                    """,
                    (
                        res["symbol"], r["accession"], cik, r["form"],
                        r["filing_date"] or None, r["report_date"],
                        r["primary_doc"], r["primary_doc_desc"], r["items"],
                        r.get("size") or 0,
                        r["doc_url"], r["is_dilution"], r["is_summary_target"], r.get("subtype"), now,
                    ),
                )
                upserted_here += 1
            except Exception as exc:
                logger.debug("upsert %s/%s: %s", sym, r.get("accession"), exc)
        upserted_total += upserted_here
        if (i + 1) % 25 == 0:
            try:
                conn.commit()
            except Exception:
                pass
            print(f"  [{i+1}/{len(targets)}] fetched={fetched} skipped={skipped} upserted={upserted_total}")
        time.sleep(delay)

    try:
        conn.commit()
    except Exception:
        pass
    conn.close()
    return fetched, upserted_total, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--penny-only", action="store_true")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--refetch-stale-hours", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.12)
    ap.add_argument("--max-rows", type=int, default=200)
    args = ap.parse_args()

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] if args.symbols else []
    conn = get_stocks_conn()
    try:
        _ensure_table(conn)
        targets = _select_targets(conn, args.penny_only, syms, args.refetch_stale_hours, args.limit)
        print(f"[targets] {len(targets)}")
    finally:
        conn.close()
    if not targets:
        return 0
    fetched, upserted, skipped = sync(targets, delay=args.delay, max_rows=args.max_rows)
    print(f"[done] fetched={fetched} upserted={upserted} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
