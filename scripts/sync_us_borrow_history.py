"""
Sync US borrow-fee/available-shares history from iBorrowDesk API into DB.

Default behavior:
- Build US symbol universe from NasdaqTrader listings (NAS/NYS/AMS)
- Fetch each symbol from https://www.iborrowdesk.com/api/ticker/{SYMBOL}
- Keep last N (default 7) daily rows
- Upsert into us_short_borrow_daily
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import requests

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from server.db.connections import get_stocks_conn

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        text = str(value).strip()
        if text in ("", "-", "None", "nan", "NaN", "null"):
            return None
        return float(text.replace(",", ""))
    except Exception:
        return None


def to_int(value: Any) -> int | None:
    v = to_float(value)
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


def normalize_symbol(symbol: str) -> str:
    s = str(symbol or "").strip().upper()
    if not s:
        return ""
    if any(ch in s for ch in ("^", "/", "$", "=", " ")):
        return ""
    s = s.replace(".", "-")
    if len(s) > 16:
        return ""
    return s


def parse_exchange_set(raw_exchanges: str) -> set[str]:
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


def download_nasdaq_table(url: str) -> list[dict]:
    resp = requests.get(url, headers=_HTTP_HEADERS, timeout=25)
    if resp.status_code != 200:
        raise RuntimeError(f"table_fetch_failed({url})={resp.status_code}")

    rows: list[dict] = []
    reader = csv.DictReader(io.StringIO(resp.text or ""), delimiter="|")
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
    nasdaq_rows = download_nasdaq_table("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt")
    other_rows = download_nasdaq_table("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt")

    symbols: list[str] = []
    seen: set[str] = set()

    if "NAS" in exchanges:
        for row in nasdaq_rows:
            if row.get("Test Issue", "").upper() == "Y":
                continue
            if (not include_etf) and row.get("ETF", "").upper() == "Y":
                continue
            norm = normalize_symbol(row.get("Symbol") or row.get("NASDAQ Symbol") or "")
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
        norm = normalize_symbol(row.get("ACT Symbol") or row.get("NASDAQ Symbol") or row.get("Symbol") or "")
        if norm and norm not in seen:
            seen.add(norm)
            symbols.append(norm)

    if max_universe > 0:
        symbols = symbols[:max_universe]
    return symbols


def ensure_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS us_short_borrow_daily (
            symbol TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            available_shares BIGINT,
            borrow_fee_pct REAL,
            rebate_rate_pct REAL,
            source TEXT,
            payload_json TEXT,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (symbol, as_of_date)
        )
        """
    )
    conn.commit()


def load_already_filled_symbols(conn, days: int) -> set[str]:
    rows = conn.execute(
        """
        SELECT symbol
        FROM us_short_borrow_daily
        GROUP BY symbol
        HAVING COUNT(*) >= ?
        """,
        (max(1, int(days)),),
    ).fetchall()
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


def rows_from_daily(symbol: str, daily: list[dict], keep_days: int) -> list[dict]:
    normalized: list[dict] = []
    for item in daily:
        if not isinstance(item, dict):
            continue
        as_of_date = str(item.get("date") or "").strip()
        if not as_of_date:
            continue
        normalized.append(
            {
                "symbol": symbol,
                "as_of_date": as_of_date,
                "available_shares": to_int(item.get("available")),
                "borrow_fee_pct": to_float(item.get("fee")),
                "rebate_rate_pct": to_float(item.get("rebate")),
                "source": "iborrowdesk",
                "payload_json": json.dumps(item, ensure_ascii=False),
                "fetched_at": utc_now_iso(),
            }
        )

    normalized.sort(key=lambda x: x["as_of_date"])
    return normalized[-max(1, int(keep_days)) :]


async def fetch_one_symbol(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    symbol: str,
    keep_days: int,
) -> dict:
    url = f"https://www.iborrowdesk.com/api/ticker/{symbol}"
    headers = {
        **_HTTP_HEADERS,
        "Referer": f"https://iborrowdesk.com/report/{symbol}",
        "Accept": "application/json",
    }

    async with sem:
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return {"symbol": symbol, "ok": False, "status": resp.status_code, "rows": []}
            payload = resp.json() or {}
            daily = payload.get("daily")
            if not isinstance(daily, list) or not daily:
                return {"symbol": symbol, "ok": False, "status": "no_daily", "rows": []}
            rows = rows_from_daily(symbol=symbol, daily=daily, keep_days=keep_days)
            return {"symbol": symbol, "ok": bool(rows), "status": "ok" if rows else "empty", "rows": rows}
        except Exception as e:
            return {"symbol": symbol, "ok": False, "status": f"error:{type(e).__name__}", "rows": []}


async def fetch_all(symbols: list[str], keep_days: int, concurrency: int, timeout_sec: float) -> list[dict]:
    sem = asyncio.Semaphore(max(1, int(concurrency)))
    timeout = httpx.Timeout(timeout_sec)
    limits = httpx.Limits(max_connections=max(20, concurrency * 2), max_keepalive_connections=max(10, concurrency))

    results: list[dict] = []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, limits=limits) as client:
        tasks = [asyncio.create_task(fetch_one_symbol(client, sem, sym, keep_days)) for sym in symbols]
        total = len(tasks)
        done = 0
        for task in asyncio.as_completed(tasks):
            result = await task
            results.append(result)
            done += 1
            if done % 100 == 0 or done == total:
                ok_count = sum(1 for r in results if r.get("ok"))
                print(f"[fetch] {done}/{total} done, ok={ok_count}")
    return results


def upsert_rows(conn, rows: list[dict]) -> int:
    if not rows:
        return 0

    params = [
        (
            row.get("symbol"),
            row.get("as_of_date"),
            row.get("available_shares"),
            row.get("borrow_fee_pct"),
            row.get("rebate_rate_pct"),
            row.get("source"),
            row.get("payload_json"),
            row.get("fetched_at"),
        )
        for row in rows
    ]

    conn.executemany(
        """
        INSERT INTO us_short_borrow_daily (
            symbol, as_of_date, available_shares, borrow_fee_pct,
            rebate_rate_pct, source, payload_json, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, as_of_date) DO UPDATE SET
            available_shares = excluded.available_shares,
            borrow_fee_pct = excluded.borrow_fee_pct,
            rebate_rate_pct = excluded.rebate_rate_pct,
            source = excluded.source,
            payload_json = excluded.payload_json,
            fetched_at = excluded.fetched_at
        """,
        params,
    )
    conn.commit()
    return len(params)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync iBorrowDesk weekly history into us_short_borrow_daily")
    parser.add_argument("--symbols", type=str, default="", help="Comma separated symbols (skip universe fetch)")
    parser.add_argument("--days", type=int, default=7, help="How many latest daily rows to keep per symbol")
    parser.add_argument("--exchanges", type=str, default="NAS,NYS,AMS", help="Universe exchanges")
    parser.add_argument("--include-etf", action="store_true", help="Include ETF symbols")
    parser.add_argument("--max-universe", type=int, default=2500, help="Max symbols to scan from universe")
    parser.add_argument("--concurrency", type=int, default=30, help="Async concurrency")
    parser.add_argument("--timeout", type=float, default=12.0, help="HTTP timeout seconds")
    parser.add_argument("--force", action="store_true", help="Force refresh even if DB already has >=days rows")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    started = time.time()

    if args.symbols.strip():
        universe = [normalize_symbol(x) for x in args.symbols.split(",")]
        symbols = [s for s in universe if s]
        source = "manual"
    else:
        exchanges = parse_exchange_set(args.exchanges)
        symbols = build_universe(exchanges=exchanges, include_etf=bool(args.include_etf), max_universe=int(args.max_universe))
        source = f"universe:{','.join(sorted(exchanges))}"

    symbols = list(dict.fromkeys(symbols))

    conn = get_stocks_conn()
    try:
        ensure_schema(conn)
        if not args.force:
            filled = load_already_filled_symbols(conn, days=args.days)
            symbols = [s for s in symbols if s not in filled]
    finally:
        conn.close()

    print("=" * 80)
    print(f"[sync_us_borrow_history] source={source} symbols={len(symbols)} days={args.days} force={args.force}")
    print("=" * 80)

    if not symbols:
        print("Nothing to fetch (all symbols already filled).")
        return

    results = asyncio.run(
        fetch_all(
            symbols=symbols,
            keep_days=max(1, int(args.days)),
            concurrency=max(1, int(args.concurrency)),
            timeout_sec=float(args.timeout),
        )
    )

    ok_symbols = [r for r in results if r.get("ok")]
    all_rows: list[dict] = []
    for result in ok_symbols:
        all_rows.extend(result.get("rows") or [])

    conn = get_stocks_conn()
    try:
        ensure_schema(conn)
        upserted = upsert_rows(conn, all_rows)
        total_rows = conn.execute("SELECT COUNT(*) FROM us_short_borrow_daily").fetchone()[0]
    finally:
        conn.close()

    elapsed = time.time() - started
    print("=" * 80)
    print(
        f"Done: symbols_total={len(symbols)}, symbols_ok={len(ok_symbols)}, "
        f"rows_upserted={upserted}, table_rows={total_rows}, elapsed={elapsed:.1f}s"
    )
    print("=" * 80)


if __name__ == "__main__":
    main()
