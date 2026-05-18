"""
sync_market_flow_intraday.py — KOSPI/KOSDAQ 시장 단위 시간대 수급을 5분 간격으로 갱신.

• KIS `inquire-investor-time-by-market` (TR `FHPTJ04030000`)을 KOSPI/KOSDAQ
  각각 호출, `market_flow_intraday` 테이블에 시계열 row 로 적재.
• KIS empty_output 빈도 높은 시간대(점심·장 외)는 그냥 skip — 다음 cron 시 재시도.
• 호출 1회 = KIS REST 2건. 5분 cron 으로 1시간당 24건 → KIS rate limit 무난.

cron: server.data_pipeline_scheduler 의 _job_market_flow_intraday 가 호출.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from collectors.kis_api import KISCollector  # noqa: E402
from server.db.connections import get_stocks_conn  # noqa: E402

# fetch_market_flow.py 의 _fetch_time 재사용 — 같은 KIS TR 호출 + 응답 파싱 로직
from scripts.fetch_market_flow import _fetch_time  # noqa: E402

logger = logging.getLogger("sync_market_flow_intraday")

_SCHEMA_READY = False


def _ensure_schema(conn) -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_flow_intraday (
            market              TEXT NOT NULL,
            fetched_at          TIMESTAMP NOT NULL,
            unit                TEXT,
            foreign_net         BIGINT,
            institution_net     BIGINT,
            individual_net      BIGINT,
            foreign_buy         BIGINT,
            foreign_sell        BIGINT,
            institution_buy     BIGINT,
            institution_sell    BIGINT,
            individual_buy      BIGINT,
            individual_sell     BIGINT,
            foreign_net_uk      INTEGER,
            institution_net_uk  INTEGER,
            individual_net_uk   INTEGER,
            index_price         DOUBLE PRECISION,
            index_change_pct    DOUBLE PRECISION,
            payload_json        TEXT,
            PRIMARY KEY (market, fetched_at)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mfi_market_time ON market_flow_intraday(market, fetched_at DESC)"
    )
    conn.commit()
    _SCHEMA_READY = True


def _upsert_row(conn, market: str, fetched_at: datetime, data: dict) -> None:
    conn.execute(
        """
        INSERT INTO market_flow_intraday (
            market, fetched_at, unit,
            foreign_net, institution_net, individual_net,
            foreign_buy, foreign_sell,
            institution_buy, institution_sell,
            individual_buy, individual_sell,
            foreign_net_uk, institution_net_uk, individual_net_uk,
            index_price, index_change_pct, payload_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (market, fetched_at) DO UPDATE SET
            foreign_net=excluded.foreign_net,
            institution_net=excluded.institution_net,
            individual_net=excluded.individual_net,
            foreign_buy=excluded.foreign_buy,
            foreign_sell=excluded.foreign_sell,
            institution_buy=excluded.institution_buy,
            institution_sell=excluded.institution_sell,
            individual_buy=excluded.individual_buy,
            individual_sell=excluded.individual_sell,
            foreign_net_uk=excluded.foreign_net_uk,
            institution_net_uk=excluded.institution_net_uk,
            individual_net_uk=excluded.individual_net_uk,
            index_price=excluded.index_price,
            index_change_pct=excluded.index_change_pct,
            payload_json=excluded.payload_json
        """,
        (
            market,
            fetched_at,
            data.get("unit"),
            data.get("foreign_net"),
            data.get("institution_net"),
            data.get("individual_net"),
            data.get("foreign_buy"),
            data.get("foreign_sell"),
            data.get("institution_buy"),
            data.get("institution_sell"),
            data.get("individual_buy"),
            data.get("individual_sell"),
            data.get("foreign_net_uk"),
            data.get("institution_net_uk"),
            data.get("individual_net_uk"),
            data.get("index_price"),
            data.get("index_change_pct"),
            json.dumps(data, ensure_ascii=False),
        ),
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        c = KISCollector()
    except Exception as e:
        logger.error("KIS init failed: %s", e)
        return 1

    fetched_at = datetime.now()
    rows: list[tuple[str, dict]] = []
    skipped: list[str] = []
    for market in ("KOSPI", "KOSDAQ"):
        try:
            data = _fetch_time(c, market)
        except Exception as e:
            logger.warning("[%s] fetch error: %s", market, e)
            skipped.append(f"{market}:exception")
            continue
        if "error" in data:
            skipped.append(f"{market}:{data['error']}")
            continue
        # 가격·수급 모두 0 이면 KIS 가 빈 응답 준 거 — 저장 안 함 (다음 cron 재시도)
        if not data.get("foreign_net") and not data.get("institution_net") and not data.get("individual_net"):
            skipped.append(f"{market}:zero_payload")
            continue
        rows.append((market, data))

    if not rows:
        logger.info("[market_flow_intra] no fresh rows (skipped=%s)", ",".join(skipped) or "-")
        return 0

    conn = get_stocks_conn()
    try:
        _ensure_schema(conn)
        for market, data in rows:
            _upsert_row(conn, market, fetched_at, data)
        conn.commit()
        for market, data in rows:
            logger.info(
                "[market_flow_intra] %s @ %s | F:%+d Ins:%+d Ind:%+d (억원)",
                market,
                fetched_at.strftime("%H:%M:%S"),
                data.get("foreign_net_uk") or 0,
                data.get("institution_net_uk") or 0,
                data.get("individual_net_uk") or 0,
            )
        if skipped:
            logger.info("[market_flow_intra] skipped: %s", ",".join(skipped))
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
