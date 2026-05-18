"""미국 종목 뉴스 + SEC 공시 sync — us_company_news 테이블 upsert.

페니 우선 sync (118 종목), 일 1회 권장.

Usage:
  python scripts/sync_us_company_news.py --penny-only
  python scripts/sync_us_company_news.py --symbols ALP,WOK,NVDA
  python scripts/sync_us_company_news.py --refetch-stale 1   # 1일 이상 안 갱신
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

from collectors.us_company_news import fetch_all_news  # noqa: E402
from server.db.connections import get_stocks_conn  # noqa: E402

logger = logging.getLogger("scripts.sync_us_company_news")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS us_company_news (
            symbol TEXT NOT NULL,
            source TEXT NOT NULL,
            url TEXT,
            title TEXT NOT NULL,
            form_type TEXT,
            publisher TEXT,
            summary TEXT,
            published_at TIMESTAMP,
            fetched_at TIMESTAMP,
            PRIMARY KEY (symbol, source, url)
        )
        """
    )
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_us_news_symbol_time ON us_company_news(symbol, published_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_us_news_published ON us_company_news(published_at DESC)")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE us_stocks ADD COLUMN IF NOT EXISTS news_updated_at TIMESTAMP")
    except Exception:
        pass
    try:
        conn.commit()
    except Exception:
        pass


def _upsert_news_batch(conn, news_list: list[dict], now: datetime) -> int:
    count = 0
    for n in news_list:
        url = n.get("url") or ""
        if not url and not n.get("title"):
            continue
        try:
            conn.execute(
                """
                INSERT INTO us_company_news
                    (symbol, source, url, title, form_type, publisher, summary, published_at, fetched_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, source, url) DO UPDATE SET
                    title = excluded.title,
                    form_type = excluded.form_type,
                    publisher = excluded.publisher,
                    summary = excluded.summary,
                    published_at = excluded.published_at,
                    fetched_at = excluded.fetched_at
                """,
                (
                    n["symbol"], n["source"], url or n["title"][:200],
                    n["title"], n.get("form_type"),
                    n.get("publisher"), n.get("summary"),
                    n.get("published_at"), now,
                ),
            )
            count += 1
        except Exception as exc:
            logger.debug("upsert %s/%s: %s", n["symbol"], url[:50], exc)
    return count


def _select_targets(conn, penny_only: bool, symbols: list[str], refetch_stale_days: int, limit: int) -> list[str]:
    if symbols:
        return symbols
    where = []
    if penny_only:
        where.append("is_penny = TRUE")
    where.append("exchange IN ('NASDAQ','NYSE','NYSE_AMEX')")
    where.append("(is_etf = FALSE OR is_etf IS NULL)")
    if refetch_stale_days > 0:
        where.append(
            f"(news_updated_at IS NULL OR news_updated_at < NOW() - INTERVAL '{int(refetch_stale_days)} days')"
        )
    where_sql = " AND ".join(where) if where else "1=1"
    sql = f"SELECT ticker FROM us_stocks WHERE {where_sql} ORDER BY is_penny DESC NULLS LAST, market_cap_usd ASC NULLS LAST"
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    cur = conn.execute(sql)
    return [r[0] for r in cur.fetchall() if r[0]]


def sync(targets: list[str], workers: int = 3) -> tuple[int, int]:
    if not targets:
        return 0, 0
    print(f"[sync_us_company_news] {len(targets)} tickers, workers={workers}")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    conn = get_stocks_conn()
    _ensure_table(conn)

    fetched_total = 0
    upserted_total = 0

    # 직렬 + 작은 worker pool (SEC rate limit 10 req/s, yfinance rate limit)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_all_news, s): s for s in targets}
        for i, fut in enumerate(as_completed(futures)):
            sym = futures[fut]
            try:
                news = fut.result()
            except Exception as exc:
                logger.debug("fetch %s failed: %s", sym, exc)
                news = []
            fetched_total += len(news)
            if news:
                upserted_total += _upsert_news_batch(conn, news, now)
                try:
                    conn.execute("UPDATE us_stocks SET news_updated_at = %s WHERE ticker = %s", (now, sym))
                except Exception:
                    pass
            if (i + 1) % 20 == 0:
                conn.commit()
                print(f"  [{i+1}/{len(targets)}] fetched={fetched_total} upserted={upserted_total}")
    conn.commit()
    conn.close()
    return fetched_total, upserted_total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--penny-only", action="store_true")
    ap.add_argument("--symbols", default="", help="콤마 구분")
    ap.add_argument("--refetch-stale", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] if args.symbols else []
    conn = get_stocks_conn()
    try:
        _ensure_table(conn)
        targets = _select_targets(conn, args.penny_only, syms, args.refetch_stale, args.limit)
        print(f"[targets] {len(targets)}")
    finally:
        conn.close()

    if not targets:
        print("no targets")
        return 0

    fetched, upserted = sync(targets, workers=args.workers)
    print(f"[done] fetched={fetched} upserted={upserted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
