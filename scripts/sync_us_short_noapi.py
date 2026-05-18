"""
Sync US short-interest and ownership metrics without paid API keys.
Sources (fallback):
- Yahoo quoteSummary (public endpoint)
- yfinance
- optional scrape fallback already in collector

This script updates:
- us_short_interest_daily
- us_ownership_daily
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from collectors.short_data_collector import ShortDataCollector
from server.db.connections import get_stocks_conn

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _normalize_symbol(symbol: str) -> str:
    s = str(symbol or "").strip().upper()
    if not s:
        return ""
    if any(ch in s for ch in ("^", "/", "$", "=", " ")):
        return ""
    s = s.replace(".", "-")
    if len(s) > 16:
        return ""
    return s


def _parse_exchange_set(raw_exchanges: str) -> set[str]:
    mapping = {
        "NASDAQ": "NAS",
        "NAS": "NAS",
        "NYSE": "NYS",
        "NYS": "NYS",
        "AMEX": "AMS",
        "AMERICAN": "AMS",
        "AMS": "AMS",
    }
    out: set[str] = set()
    for item in str(raw_exchanges or "").split(","):
        token = mapping.get(str(item).strip().upper())
        if token:
            out.add(token)
    if not out:
        out = {"NAS", "NYS", "AMS"}
    return out


def _download_table(url: str) -> list[dict]:
    res = requests.get(url, headers=_HTTP_HEADERS, timeout=25)
    if res.status_code != 200:
        raise RuntimeError(f"table_fetch_failed({url})={res.status_code}")
    rows: list[dict] = []
    reader = csv.DictReader(io.StringIO(res.text or ""), delimiter="|")
    for row in reader:
        if not row:
            continue
        first_key = next(iter(row.keys()), "")
        first_val = str(row.get(first_key, "")).strip()
        if first_val.lower().startswith("file creation time"):
            break
        rows.append({str(k).strip(): str(v or "").strip() for k, v in row.items() if k is not None})
    return rows


def build_universe(exchanges: set[str], include_etf: bool, max_universe: int) -> list[str]:
    nasdaq_rows = _download_table("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt")
    other_rows = _download_table("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt")

    symbols: list[str] = []
    seen: set[str] = set()

    if "NAS" in exchanges:
        for row in nasdaq_rows:
            if row.get("Test Issue", "").upper() == "Y":
                continue
            if (not include_etf) and row.get("ETF", "").upper() == "Y":
                continue
            norm = _normalize_symbol(row.get("Symbol") or row.get("NASDAQ Symbol") or "")
            if norm and norm not in seen:
                seen.add(norm)
                symbols.append(norm)

    for row in other_rows:
        exchange_code = str(row.get("Exchange") or "").upper()
        mapped = {"N": "NYS", "A": "AMS", "P": "AMS"}.get(exchange_code)
        if mapped not in exchanges:
            continue
        if row.get("Test Issue", "").upper() == "Y":
            continue
        if (not include_etf) and row.get("ETF", "").upper() == "Y":
            continue
        norm = _normalize_symbol(row.get("ACT Symbol") or row.get("NASDAQ Symbol") or row.get("Symbol") or "")
        if norm and norm not in seen:
            seen.add(norm)
            symbols.append(norm)

    if max_universe > 0:
        symbols = symbols[:max_universe]
    return symbols


def load_already_synced_symbols(today_iso: str) -> set[str]:
    conn = get_stocks_conn()
    try:
        rows = conn.execute(
            """
            SELECT symbol
            FROM us_short_interest_daily
            WHERE as_of_date = ?
            """,
            (today_iso,),
        ).fetchall()
    except Exception:
        return set()
    finally:
        conn.close()

    out: set[str] = set()
    for row in rows:
        try:
            out.add(str(row[0]).upper())
        except Exception:
            try:
                out.add(str(row["symbol"]).upper())
            except Exception:
                pass
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="No-key US short/ownership sync")
    parser.add_argument("--symbols", type=str, default="", help="Comma separated symbols")
    parser.add_argument("--exchanges", type=str, default="NAS,NYS,AMS")
    parser.add_argument("--include-etf", action="store_true")
    parser.add_argument("--max-universe", type=int, default=2500)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    started = time.time()

    if args.symbols.strip():
        symbols = [_normalize_symbol(x) for x in args.symbols.split(",")]
        symbols = [s for s in symbols if s]
        src = "manual"
    else:
        ex = _parse_exchange_set(args.exchanges)
        symbols = build_universe(exchanges=ex, include_etf=bool(args.include_etf), max_universe=int(args.max_universe))
        src = f"universe:{','.join(sorted(ex))}"

    symbols = list(dict.fromkeys(symbols))

    today = _today_iso()
    if not args.force:
        synced = load_already_synced_symbols(today)
        symbols = [s for s in symbols if s not in synced]

    print("=" * 80)
    print(f"[sync_us_short_noapi] source={src} symbols={len(symbols)} workers={args.workers} force={args.force}")
    print("=" * 80)

    if not symbols:
        print("Nothing to sync.")
        return

    collector = ShortDataCollector()

    def _job(sym: str) -> dict:
        short_data = collector.get_short_metrics(sym, force_refresh=True)
        own_data = collector.get_ownership_metrics(sym, force_refresh=True)
        short_ok = any(short_data.get(k) is not None for k in ("short_float_pct", "short_interest_shares", "days_to_cover"))
        own_ok = any(own_data.get(k) is not None for k in ("institutional_ownership_pct", "insider_ownership_pct"))
        return {
            "symbol": sym,
            "short_ok": bool(short_ok),
            "own_ok": bool(own_ok),
            "short_source": short_data.get("source"),
            "own_source": own_data.get("source"),
        }

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, int(args.workers))) as ex:
        futures = {ex.submit(_job, sym): sym for sym in symbols}
        total = len(futures)
        done = 0
        for future in as_completed(futures):
            done += 1
            try:
                results.append(future.result())
            except Exception:
                results.append({"symbol": futures[future], "short_ok": False, "own_ok": False})
            if done % 100 == 0 or done == total:
                short_ok_count = sum(1 for r in results if r.get("short_ok"))
                own_ok_count = sum(1 for r in results if r.get("own_ok"))
                print(f"[sync] {done}/{total} short_ok={short_ok_count} own_ok={own_ok_count}")

    short_ok_count = sum(1 for r in results if r.get("short_ok"))
    own_ok_count = sum(1 for r in results if r.get("own_ok"))
    elapsed = time.time() - started

    print("=" * 80)
    print(
        f"Done: total={len(symbols)} short_ok={short_ok_count} own_ok={own_ok_count} "
        f"elapsed={elapsed:.1f}s"
    )
    print("=" * 80)


if __name__ == "__main__":
    main()
