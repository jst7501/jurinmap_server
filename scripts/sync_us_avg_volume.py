"""us_stocks.avg_volume_10d / avg_volume_3m / float_shares / shares_outstanding 보강.

yfinance fast_info — Ticker.info 보다 10배 빠르고 페니 일부에서도 응답 OK.
- fast_info.tenDayAverageVolume
- fast_info.threeMonthAverageVolume
- fast_info.shares
- yfinance.info.floatShares (없으면 None)

NULL 종목만 채움. ~60 종목 / 4 worker → 약 1-2분.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

from server.db.connections import get_stocks_conn  # noqa: E402

logger = logging.getLogger("scripts.sync_us_avg_volume")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def fetch_volume_meta(symbol: str) -> dict | None:
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        t = yf.Ticker(symbol)
        fi = dict(t.fast_info or {})
    except Exception as exc:
        logger.debug("fast_info %s: %s", symbol, exc)
        return None
    if not fi:
        return None
    out = {
        "avg_volume_10d": int(fi.get("tenDayAverageVolume") or 0) or None,
        "avg_volume_3m": int(fi.get("threeMonthAverageVolume") or 0) or None,
        "shares_outstanding": int(fi.get("shares") or 0) or None,
        "float_shares": None,
    }
    # float_shares 는 info 에서 (느림, 페니 자주 None)
    try:
        info = t.info or {}
        fs = info.get("floatShares")
        if fs:
            out["float_shares"] = int(fs)
    except Exception:
        pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--penny-only", action="store_true", default=True)
    ap.add_argument("--null-only", action="store_true", default=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = get_stocks_conn()
    where = ["exchange IN ('NASDAQ','NYSE','NYSE_AMEX')", "(is_etf = FALSE OR is_etf IS NULL)", "length(ticker) <= 5"]
    if args.penny_only:
        where.append("is_penny = TRUE")
    if args.null_only:
        where.append("(avg_volume_10d IS NULL OR avg_volume_10d = 0 OR shares_outstanding IS NULL OR shares_outstanding = 0)")
    sql = f"SELECT ticker FROM us_stocks WHERE {' AND '.join(where)} ORDER BY market_cap_usd DESC NULLS LAST"
    if args.limit > 0:
        sql += f" LIMIT {int(args.limit)}"
    cur = conn.execute(sql)
    targets = [r[0] for r in cur.fetchall() if r and r[0]]
    print(f"[targets] {len(targets)}")
    if not targets:
        return 0

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    updated = 0
    no_data = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_volume_meta, s): s for s in targets}
        for i, fut in enumerate(as_completed(futures)):
            sym = futures[fut]
            try:
                meta = fut.result()
            except Exception as exc:
                meta = None
                logger.debug("err %s: %s", sym, exc)
            if not meta or not (meta.get("avg_volume_10d") or meta.get("shares_outstanding")):
                no_data += 1
                continue
            try:
                conn.execute(
                    """
                    UPDATE us_stocks SET
                        avg_volume_10d = COALESCE(NULLIF(avg_volume_10d, 0), %s),
                        avg_volume_3m = COALESCE(NULLIF(avg_volume_3m, 0), %s),
                        shares_outstanding = COALESCE(NULLIF(shares_outstanding, 0), %s),
                        float_shares = COALESCE(NULLIF(float_shares, 0), %s)
                    WHERE ticker = %s
                    """,
                    (meta.get("avg_volume_10d"), meta.get("avg_volume_3m"),
                     meta.get("shares_outstanding"), meta.get("float_shares"), sym),
                )
                updated += 1
            except Exception as exc:
                logger.debug("upsert %s: %s", sym, exc)
            if (i + 1) % 20 == 0:
                try:
                    conn.commit()
                except Exception:
                    pass
                print(f"  [{i+1}/{len(targets)}] updated={updated} no_data={no_data}")

    try:
        conn.commit()
    except Exception:
        pass
    conn.close()
    print(f"[done] updated={updated} no_data={no_data}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
