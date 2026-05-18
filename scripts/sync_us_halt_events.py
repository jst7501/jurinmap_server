"""US trade halts → DB 영속화.

NASDAQ Trader RSS 는 ~24시간 윈도우만 보여줌. cron 으로 5분마다 실행해서
보이는 halt 를 DB 에 쌓아두면 종목별 halt 이력을 시간에 따라 축적.
us_halt_events PK = (symbol, halt_at_utc) — 중복 자동 dedupe.

사용:
    python scripts/sync_us_halt_events.py
    python scripts/sync_us_halt_events.py --hours 48

cron 예: */5 * * * * python /path/scripts/sync_us_halt_events.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

from collectors.us_trade_halts import get_recent_halts  # noqa: E402
from server.db.connections import get_stocks_conn  # noqa: E402

logger = logging.getLogger("scripts.sync_us_halt_events")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS us_halt_events (
            symbol TEXT NOT NULL,
            halt_at_utc TIMESTAMP NOT NULL,
            expected_resume_at_utc TIMESTAMP,
            reason_code TEXT,
            reason_kr TEXT,
            halt_type TEXT,
            market_category TEXT,
            name TEXT,
            pause_threshold_price NUMERIC(20, 6),
            captured_at TIMESTAMP,
            PRIMARY KEY (symbol, halt_at_utc)
        )
        """
    )
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_halt_events_symbol ON us_halt_events(symbol, halt_at_utc DESC)",
        "CREATE INDEX IF NOT EXISTS idx_halt_events_recent ON us_halt_events(halt_at_utc DESC)",
        "CREATE INDEX IF NOT EXISTS idx_halt_events_reason ON us_halt_events(reason_code, halt_at_utc DESC)",
    ]:
        try:
            conn.execute(idx)
        except Exception:
            pass
    try:
        conn.commit()
    except Exception:
        pass


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def sync(hours: int = 24) -> tuple[int, int]:
    halts = get_recent_halts(active_only=False, hours=hours, max_items=500)
    if not halts:
        print("[no halts in feed]")
        return 0, 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    conn = get_stocks_conn()
    _ensure_table(conn)
    upserted = 0
    skipped = 0
    for h in halts:
        try:
            halt_utc = _parse_dt(h.get("halt_at_utc"))
            if not halt_utc:
                skipped += 1
                continue
            resume_utc = _parse_dt(h.get("expected_resume_at_utc"))
            conn.execute(
                """
                INSERT INTO us_halt_events (
                    symbol, halt_at_utc, expected_resume_at_utc,
                    reason_code, reason_kr, halt_type, market_category,
                    name, pause_threshold_price, captured_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(symbol, halt_at_utc) DO UPDATE SET
                    expected_resume_at_utc = COALESCE(EXCLUDED.expected_resume_at_utc, us_halt_events.expected_resume_at_utc),
                    reason_code = EXCLUDED.reason_code,
                    reason_kr = EXCLUDED.reason_kr,
                    halt_type = EXCLUDED.halt_type,
                    market_category = EXCLUDED.market_category,
                    name = COALESCE(EXCLUDED.name, us_halt_events.name)
                """,
                (
                    (h.get("symbol") or "").upper(),
                    halt_utc,
                    resume_utc,
                    h.get("reason_code"),
                    h.get("reason_kr"),
                    h.get("halt_type"),
                    h.get("market_category"),
                    h.get("name"),
                    h.get("pause_threshold_price"),
                    now,
                ),
            )
            upserted += 1
        except Exception as exc:
            logger.debug("upsert %s: %s", h.get("symbol"), exc)
            skipped += 1
    conn.commit()
    conn.close()
    return upserted, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    args = ap.parse_args()
    up, sk = sync(args.hours)
    print(f"[done] upserted={up} skipped={sk}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
