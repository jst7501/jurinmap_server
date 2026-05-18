"""
sync_stocks_meta_backfill.py
───────────────────────────────────────────────────────────────────
`price_today` 에 시세는 들어오는데 `stocks` 마스터에 메타(name/market)가 없는
신규 상장·재상장 종목을 자동 backfill.

데이터 소스:
- KIS `search_stock_info` TR (CTPF1002R) — 종목 정확한 한국어 이름 + 시장 코드.
  6자리 정상 코드 / Q접두 ETN / F접두 펀드 / K·L 접미 우선주 모두 지원.
- price_today.raw_json 의 `rprs_mrkt_kor_name` — 한국어 시장명 (KIS 가 dash 로
  돌려주는 케이스 fallback).

운영:
- data_pipeline_scheduler 에 등록 (매일 09:30 / 16:30).
- 첫 실행: 깨진 한글 / placeholder 모두 정확한 KIS 데이터로 덮어씀.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from typing import Any

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)


from server.db.connections import get_stocks_conn  # noqa: E402
from collectors.kis_api import KISCollector  # noqa: E402


# KIS mket_id_cd → 한국어 시장명
_MKT_CODE_MAP = {
    "STK": "코스피",
    "KSP": "코스피",
    "KSQ": "코스닥",
    "KSX": "코넥스",
}


def _now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _market_from_raw(raw: Any) -> str | None:
    """price_today.raw_json 의 rprs_mrkt_kor_name → 한국어 시장명."""
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, dict):
        return None
    m = (raw.get("rprs_mrkt_kor_name") or "").strip()
    if m == "KOSPI":
        return "코스피"
    if m == "KOSDAQ":
        return "코스닥"
    if "ETF" in m:
        return "ETF"
    if "ETN" in m:
        return "ETN"
    if "KONEX" in m or "코넥스" in m:
        return "코넥스"
    return m or None


def _resolve_market(kis_market_code: str, raw_market: str | None) -> str:
    """우선순위: price_today raw 의 ETF/ETN (가장 정확) > KIS 코드 매핑 > raw 그대로."""
    # ETF/ETN 은 raw_market 에서만 알 수 있음 (KIS 는 STK 로 표시)
    if raw_market in ("ETF", "ETN"):
        return raw_market
    mapped = _MKT_CODE_MAP.get((kis_market_code or "").strip().upper())
    if mapped:
        return mapped
    if raw_market:
        return raw_market
    return "기타"


def main() -> int:
    conn = get_stocks_conn()
    try:
        miss = conn.execute(
            """
            SELECT pt.code, pt.raw_json
            FROM price_today pt
            LEFT JOIN stocks s ON s.code = pt.code
            WHERE s.code IS NULL
            ORDER BY pt.updated_at DESC
            """
        ).fetchall()

        # 추가로 placeholder/깨진 한글 보정 — 옵션 환경변수
        if os.getenv("STOCKS_META_REPAIR", "0") in ("1", "true", "TRUE"):
            broken = conn.execute(
                """
                SELECT pt.code, pt.raw_json
                FROM price_today pt
                LEFT JOIN stocks s ON s.code = pt.code
                WHERE s.code IS NOT NULL
                  AND (s.name LIKE ? OR s.name LIKE ?)
                """,
                ("[신규%", "%���%"),  # placeholder + 깨진 surrogate
            ).fetchall()
            miss = list(miss) + list(broken)

        if not miss:
            print("[sync_stocks_meta_backfill] no missing rows — all stocks have meta")
            return 0

        kis = KISCollector()
        ts = _now_ts()
        n_ok, n_fail = 0, 0

        for r in miss:
            d = dict(r)
            code = d["code"]
            raw_market = _market_from_raw(d.get("raw_json"))

            # KIS 호출. prdt_type_cd=300 (주식/ETF/ETN/ELW 통합)
            info = kis.get_stock_master(code, "300")
            name = (info.get("name") or "").strip()
            kis_market = (info.get("market") or "").strip()

            if name and name != "-":
                market = _resolve_market(kis_market, raw_market)
                n_ok += 1
            else:
                name = f"[신규 {code}]"
                market = raw_market or "기타"
                n_fail += 1

            conn.execute(
                """
                INSERT INTO stocks(code, name, market, updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(code) DO UPDATE SET
                  name = excluded.name,
                  market = excluded.market,
                  updated_at = excluded.updated_at
                """,
                (code, name, market, ts),
            )
            # KIS rate-limit 보호 (초당 ~20건)
            time.sleep(0.06)

        conn.commit()
        print(
            f"[sync_stocks_meta_backfill] processed={len(miss)} "
            f"kis_resolved={n_ok} placeholder={n_fail}"
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
