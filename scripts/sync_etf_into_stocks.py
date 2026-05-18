"""
sync_etf_into_stocks.py
───────────────────────────────────────────────────────────────────
`kr_etf_master` 의 ETF 를 `stocks` + `price_today` 에 통합.

목적: ETF 를 일반 종목과 분리하지 않고 종목 검색·거래대금/시장 리스트에
함께 노출. `/api/stocks/search` 와 `/api/stocks/list` 가 모두
`stocks LEFT JOIN price_today` 기반이므로, ETF 를 두 테이블에 넣으면
별도 라우트 수정 없이 자동 노출.

매핑 (kr_etf_master → price_today):
  price        → current_price
  change_rate  → change_pct
  change_amt   → change_amt
  volume       → trading_volume
  amount       → trading_value
  market_cap   → market_cap

stocks 에는 market='ETF' 로 표시 (프론트에서 ETF 배지·라우팅 분기용).

운영: ETF 폴러(part02 _etf_background_poller)가 kr_etf_master 를 갱신하면
이 스크립트가 stocks/price_today 로 전파. data_pipeline_scheduler 등록.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)


from server.db.connections import get_stocks_conn  # noqa: E402


def _now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    conn = get_stocks_conn()
    try:
        etfs = conn.execute(
            """
            SELECT code, name, price, change_rate, change_amt,
                   volume, amount, market_cap
            FROM kr_etf_master
            WHERE code IS NOT NULL AND name IS NOT NULL AND name != ''
            """
        ).fetchall()

        if not etfs:
            print("[sync_etf_into_stocks] kr_etf_master empty — nothing to sync")
            return 0

        ts = _now_ts()
        n_stocks, n_price = 0, 0

        for r in etfs:
            d = dict(r)
            code = d["code"]
            name = (d.get("name") or "").strip()
            if not code or not name:
                continue

            # 1) stocks 마스터 — market='ETF'
            conn.execute(
                """
                INSERT INTO stocks(code, name, market, updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(code) DO UPDATE SET
                  name = excluded.name,
                  market = 'ETF',
                  updated_at = excluded.updated_at
                """,
                (code, name, "ETF", ts),
            )
            n_stocks += 1

            # 2) price_today — kr_etf_master 시세 매핑
            #    ETF 는 per/pbr/eps/foreign_hold_pct/listed_shares 없음 → NULL
            conn.execute(
                """
                INSERT INTO price_today(
                    code, current_price, change_pct, change_amt,
                    trading_value, trading_volume, market_cap, updated_at
                )
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(code) DO UPDATE SET
                  current_price  = excluded.current_price,
                  change_pct     = excluded.change_pct,
                  change_amt     = excluded.change_amt,
                  trading_value  = excluded.trading_value,
                  trading_volume = excluded.trading_volume,
                  market_cap     = excluded.market_cap,
                  updated_at     = excluded.updated_at
                """,
                (
                    code,
                    d.get("price"),
                    d.get("change_rate"),
                    d.get("change_amt"),
                    d.get("amount"),       # trading_value
                    d.get("volume"),       # trading_volume
                    d.get("market_cap"),
                    ts,
                ),
            )
            n_price += 1

        conn.commit()
        print(
            f"[sync_etf_into_stocks] etfs={len(etfs)} "
            f"stocks_upsert={n_stocks} price_today_upsert={n_price}"
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
