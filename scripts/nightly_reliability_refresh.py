"""
Nightly reliability refresh for domestic market data.

Targets:
- program_trade: code missing target day row
- short_data: stale or zero ratio
- credit_data: stale or empty daily payload
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from collectors.kis_api import KISCollector, safe_float
from server.db.connections import get_stocks_conn


def _now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _as_json(v) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _fetch_code_set(conn, sql: str, params: tuple) -> set[str]:
    out = set()
    rows = conn.execute(sql, params).fetchall()
    for r in rows:
        try:
            code = str(r[0]).zfill(6)
        except Exception:
            continue
        if code:
            out.add(code)
    return out


def _upsert_short(conn, code: str, ratio: float, ts: str):
    conn.execute(
        """
        INSERT INTO short_data(code, short_enabled, short_selling_volume_ratio, updated_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
          short_enabled=excluded.short_enabled,
          short_selling_volume_ratio=excluded.short_selling_volume_ratio,
          updated_at=excluded.updated_at
        """,
        (code, 1 if ratio > 0 else 0, ratio, ts),
    )


def _upsert_credit(conn, code: str, daily_rows: list[dict], ts: str):
    rate_today = 0.0
    if daily_rows:
        rate_today = float(daily_rows[0].get("credit_rate") or 0.0)
    conn.execute(
        """
        INSERT INTO credit_data(code, rate_today, daily_json, updated_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
          rate_today=excluded.rate_today,
          daily_json=excluded.daily_json,
          updated_at=excluded.updated_at
        """,
        (code, rate_today, _as_json(daily_rows), ts),
    )


def _upsert_program(conn, code: str, rows: list[dict]):
    for r in rows:
        date = str(r.get("date") or "").strip()
        if not date:
            continue
        conn.execute("DELETE FROM program_trade WHERE code=? AND date=?", (code, date))
        conn.execute(
            """
            INSERT INTO program_trade(code, date, program_buy, program_sell, program_net, program_net_amt)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                code,
                date,
                int(r.get("program_buy") or 0),
                int(r.get("program_sell") or 0),
                int(r.get("program_net") or 0),
                int(r.get("program_net_amt") or 0),
            ),
        )


def _fetch_with_retry(fetch_fn, ok_fn, retries: int = 2, base_delay: float = 0.7):
    last = None
    for attempt in range(retries + 1):
        try:
            val = fetch_fn()
            last = val
            if ok_fn(val):
                return val, True
        except Exception:
            last = None
        if attempt < retries:
            time.sleep(base_delay * (attempt + 1))
    return last, False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="", help="Target trading date (YYYYMMDD). Default: today")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of target codes (0 = all)")
    parser.add_argument("--sleep", type=float, default=0.20, help="Delay between symbols (seconds)")
    parser.add_argument("--batch", type=int, default=50, help="Commit interval")
    args = parser.parse_args()

    target_date = (args.date or "").strip() or datetime.now().strftime("%Y%m%d")
    target_date_dash = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:8]}"
    ts = _now_ts()

    conn = get_stocks_conn()
    try:
        program_targets = _fetch_code_set(
            conn,
            """
            SELECT s.code
            FROM stocks s
            LEFT JOIN (
              SELECT code, MAX(date) AS max_date
              FROM program_trade
              GROUP BY code
            ) p ON p.code = s.code
            WHERE COALESCE(p.max_date, '') < ?
            """,
            (target_date,),
        )
        short_targets = _fetch_code_set(
            conn,
            """
            SELECT s.code
            FROM stocks s
            LEFT JOIN short_data sd ON sd.code = s.code
            WHERE sd.code IS NULL
               OR COALESCE(sd.short_selling_volume_ratio, 0) <= 0
               OR SUBSTR(COALESCE(sd.updated_at, ''), 1, 10) < ?
            """,
            (target_date_dash,),
        )
        credit_targets = _fetch_code_set(
            conn,
            """
            SELECT s.code
            FROM stocks s
            LEFT JOIN credit_data cd ON cd.code = s.code
            WHERE cd.code IS NULL
               OR COALESCE(cd.rate_today, 0) <= 0
               OR COALESCE(cd.daily_json, '') IN ('', '[]')
               OR SUBSTR(COALESCE(cd.updated_at, ''), 1, 10) < ?
            """,
            (target_date_dash,),
        )

        need_map: dict[str, dict] = {}
        for c in program_targets:
            need_map.setdefault(c, {})["program"] = True
        for c in short_targets:
            need_map.setdefault(c, {})["short"] = True
        for c in credit_targets:
            need_map.setdefault(c, {})["credit"] = True

        targets = sorted(need_map.keys())
        if args.limit > 0:
            targets = targets[: args.limit]

        print("=" * 72)
        print(f"[nightly_reliability_refresh] target_date={target_date} total_targets={len(targets)}")
        print(f"breakdown: program={len(program_targets)} short={len(short_targets)} credit={len(credit_targets)}")
        print("=" * 72)

        kis = KISCollector()
        stats = {
            "target": len(targets),
            "processed": 0,
            "program_ok": 0,
            "program_fail": 0,
            "short_ok": 0,
            "short_fail": 0,
            "credit_ok": 0,
            "credit_fail": 0,
        }

        started = time.time()
        for idx, code in enumerate(targets, 1):
            need = need_map.get(code, {})
            try:
                if need.get("program"):
                    program, ok = _fetch_with_retry(
                        fetch_fn=lambda: (kis.get_program_trade_5d(code) or []),
                        ok_fn=lambda v: isinstance(v, list) and len(v) > 0,
                        retries=2,
                        base_delay=0.8,
                    )
                    if ok:
                        _upsert_program(conn, code, program)
                        stats["program_ok"] += 1
                    else:
                        stats["program_fail"] += 1

                if need.get("short"):
                    short_data, ok = _fetch_with_retry(
                        fetch_fn=lambda: (kis.get_short_sale(code, 5) or {}),
                        ok_fn=lambda v: (
                            isinstance(v, dict)
                            and (
                                safe_float(v.get("short_selling_volume_ratio"), 0.0) > 0
                                or len(v.get("daily") or []) > 0
                            )
                        ),
                        retries=2,
                        base_delay=0.8,
                    )
                    if ok:
                        ratio = safe_float(short_data.get("short_selling_volume_ratio"), 0.0)
                        _upsert_short(conn, code, ratio, ts)
                        stats["short_ok"] += 1
                    else:
                        stats["short_fail"] += 1

                if need.get("credit"):
                    credit_rows, ok = _fetch_with_retry(
                        fetch_fn=lambda: (kis.get_credit_balance(code) or []),
                        ok_fn=lambda v: isinstance(v, list) and len(v) > 0,
                        retries=2,
                        base_delay=0.8,
                    )
                    if ok:
                        _upsert_credit(conn, code, credit_rows, ts)
                        stats["credit_ok"] += 1
                    else:
                        stats["credit_fail"] += 1
            finally:
                stats["processed"] += 1

            if (idx % max(1, args.batch) == 0) or (idx == len(targets)):
                conn.commit()
                elapsed = time.time() - started
                print(
                    f"  - {idx}/{len(targets)} done"
                    f" | p_ok={stats['program_ok']} s_ok={stats['short_ok']} c_ok={stats['credit_ok']}"
                    f" | {elapsed:.1f}s"
                )

            if args.sleep > 0:
                time.sleep(args.sleep)

        conn.commit()
        elapsed = time.time() - started
        summary = {
            **stats,
            "target_date": target_date,
            "elapsed_sec": round(elapsed, 2),
            "finished_at": _now_ts(),
        }
        print("SUMMARY_JSON=" + _as_json(summary))
        print("=" * 72)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
