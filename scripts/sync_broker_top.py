"""
sync_broker_top.py — 종목별 거래원 매수/매도 Top5 를 매일 마감 후 1회 수집.

• KIS inquire-member (TR FHKST01010500) — 종목당 1 호출
• 대상 종목: data/top_100_trade_value.json + 메가캡 보존 (시총 상위)
• PG 테이블 broker_trade_top — (code, date, side, rank) PK 시계열
• cron: data_pipeline_scheduler 의 _job_broker_top_daily (16:35)

KIS rate limit 회피: workers=2, sleep=0.15 — 약 3000 종목 25분 (full 모드).
기본은 거래대금 상위 100~300만 수집 → 1~2분.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Iterable

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from collectors.kis_api import KISCollector  # noqa: E402
from server.db.connections import get_stocks_conn  # noqa: E402

logger = logging.getLogger("sync_broker_top")

_SCHEMA_READY = False


def _ensure_schema(conn) -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS broker_trade_top (
            code         TEXT NOT NULL,
            date         TEXT NOT NULL,
            side         TEXT NOT NULL,        -- 'buy' | 'sell'
            rank         INTEGER NOT NULL,     -- 1..5
            broker_name  TEXT NOT NULL,
            broker_no    TEXT,
            qty          BIGINT,
            qty_change   BIGINT,
            is_foreign   BOOLEAN NOT NULL DEFAULT FALSE,
            fetched_at   TEXT NOT NULL,
            PRIMARY KEY (code, date, side, rank)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_broker_top_code_date ON broker_trade_top(code, date DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_broker_top_broker ON broker_trade_top(broker_name, date DESC)")
    conn.commit()
    _SCHEMA_READY = True


def _target_codes(mode: str, limit: int) -> list[str]:
    """수집 대상 종목.
    mode='top': data/top_100_trade_value.json + 메가캡 (시총 상위 50)
    mode='full': stocks 테이블 전체 (~3000개)
    """
    codes: list[str] = []
    if mode == "top":
        path = ROOT_DIR / "data" / "top_100_trade_value.json"
        try:
            with open(path, encoding="utf-8") as f:
                items = json.load(f)
            if isinstance(items, list):
                for it in items:
                    code = str(it.get("code") or it.get("symbol") or "").zfill(6)
                    if code and code != "000000":
                        codes.append(code)
        except Exception as e:
            logger.warning("top_100_trade_value.json read fail: %s", e)

        # 메가캡 보존 — 시총 상위 50종 (상위 100 거래대금에 안 들어와도 추적할 가치)
        try:
            conn = get_stocks_conn()
            try:
                rows = conn.execute(
                    """
                    SELECT pt.code FROM price_today pt
                    WHERE pt.market_cap > 0
                    ORDER BY pt.market_cap DESC LIMIT 50
                    """
                ).fetchall()
                for r in rows:
                    code = str(r[0] if not hasattr(r, "keys") else r["code"]).zfill(6)
                    if code not in codes:
                        codes.append(code)
            finally:
                conn.close()
        except Exception as e:
            logger.warning("mega_cap fetch fail: %s", e)
    else:  # 'full'
        conn = get_stocks_conn()
        try:
            rows = conn.execute("SELECT code FROM stocks ORDER BY code").fetchall()
            codes = [str(r[0] if not hasattr(r, "keys") else r["code"]).zfill(6) for r in rows]
        finally:
            conn.close()

    if limit > 0:
        codes = codes[:limit]
    return codes


def _fetch_one(c: KISCollector, code: str) -> dict | None:
    try:
        return c.get_member_trade(code)
    except Exception as e:
        logger.debug("[broker] %s fetch error: %s", code, e)
        return None


def _upsert_one(conn, today: str, fetched_at: str, payload: dict) -> int:
    code = payload.get("code") or ""
    if not code or "error" in payload:
        return 0
    rows: list[tuple] = []
    for side in ("buy", "sell"):
        for entry in payload.get(side) or []:
            rows.append(
                (
                    code,
                    today,
                    side,
                    int(entry.get("rank") or 0),
                    str(entry.get("broker_name") or ""),
                    str(entry.get("broker_no") or ""),
                    int(entry.get("qty") or 0),
                    int(entry.get("qty_change") or 0),
                    bool(entry.get("is_foreign")),
                    fetched_at,
                )
            )
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO broker_trade_top (
            code, date, side, rank, broker_name, broker_no,
            qty, qty_change, is_foreign, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (code, date, side, rank) DO UPDATE SET
            broker_name = excluded.broker_name,
            broker_no   = excluded.broker_no,
            qty         = excluded.qty,
            qty_change  = excluded.qty_change,
            is_foreign  = excluded.is_foreign,
            fetched_at  = excluded.fetched_at
        """,
        rows,
    )
    return len(rows)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["top", "full"], default="top",
                    help="top=상위 거래대금+메가캡(~150종목, 1-2분), full=전체(~3000종목, 25분)")
    ap.add_argument("--limit", type=int, default=0, help="대상 종목 상한 (0=무제한)")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--sleep", type=float, default=0.15, help="호출 간 sleep (KIS rate limit)")
    args = ap.parse_args()

    codes = _target_codes(args.mode, args.limit)
    if not codes:
        logger.warning("no target codes")
        return 1
    logger.info("[broker_top] start mode=%s targets=%d workers=%d", args.mode, len(codes), args.workers)

    today = datetime.now().strftime("%Y-%m-%d")
    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c = KISCollector()

    success = 0
    failed = 0
    rows_total = 0

    conn = get_stocks_conn()
    try:
        _ensure_schema(conn)
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            futures = {}
            for code in codes:
                f = ex.submit(_fetch_one, c, code)
                futures[f] = code
                time.sleep(args.sleep)
            for f in as_completed(futures):
                code = futures[f]
                payload = f.result()
                if not payload or "error" in (payload or {}):
                    failed += 1
                    continue
                try:
                    n = _upsert_one(conn, today, fetched_at, payload)
                    rows_total += n
                    success += 1
                except Exception as e:
                    logger.warning("[broker] %s upsert fail: %s", code, e)
                    failed += 1
        conn.commit()
    finally:
        conn.close()

    logger.info(
        "[broker_top] done targets=%d success=%d failed=%d rows=%d",
        len(codes), success, failed, rows_total,
    )
    return 0 if success else 2


if __name__ == "__main__":
    sys.exit(main())
