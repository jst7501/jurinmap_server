"""
SEC 8-K / 6-K AI 요약 **결과를 DB에 기록**.

Claude Code 루틴이 자체 생성한 한국어 한 줄 요약을 저장.
AI API 호출 X.

사용법:
    python scripts/ai_filing_summary_upsert.py \
      --symbol ALP --accession 0001234567-26-000001 \
      --one-liner "신주 인수계약 체결 — 발행 주식 300만주 약 $2M 조달. dilution 5%." \
      --tone risk \
      --drivers "신주인수계약,dilution,$2M 조달"

실패:
    python scripts/ai_filing_summary_upsert.py --symbol XXX --accession ... \
      --status error --error "no_text"

카피 규약 (페니 단타 타겟):
  - 한 줄 35-80자
  - "~요/~에요" 자연스럽게. 명령·단정 금지.
  - 숫자는 본문에 있는 것만 (지어내지 않기)
  - 행위 권유 금지 ("매수", "팔아야"). 허용: "참고", "주의", "관찰"
  - tone enum: up (호재) / down (악재) / neutral / risk (dilution·임원 변경 등)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.db.connections import get_stocks_conn  # noqa: E402

MODEL_TAG = "claude-code-routine"


def _now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_table(con: Any) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS us_filing_summaries (
            symbol TEXT NOT NULL,
            accession TEXT NOT NULL,
            one_liner TEXT,
            drivers_json TEXT,
            tone TEXT,
            model TEXT,
            status TEXT,
            error TEXT,
            updated_at TIMESTAMP,
            PRIMARY KEY (symbol, accession)
        )
        """
    )
    try:
        con.execute("CREATE INDEX IF NOT EXISTS idx_us_filing_summaries_updated ON us_filing_summaries(updated_at DESC)")
    except Exception:
        pass


def _split_csv(s: str | None) -> list[str]:
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


def upsert(con: Any, args) -> None:
    _ensure_table(con)
    con.execute(
        """
        INSERT INTO us_filing_summaries
            (symbol, accession, one_liner, drivers_json, tone,
             model, status, error, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(symbol, accession) DO UPDATE SET
            one_liner = EXCLUDED.one_liner,
            drivers_json = EXCLUDED.drivers_json,
            tone = EXCLUDED.tone,
            model = EXCLUDED.model,
            status = EXCLUDED.status,
            error = EXCLUDED.error,
            updated_at = EXCLUDED.updated_at
        """,
        (
            args.symbol.upper(),
            args.accession,
            args.one_liner or "",
            json.dumps(_split_csv(args.drivers), ensure_ascii=False),
            args.tone or "",
            MODEL_TAG,
            args.status or "ok",
            args.error or None,
            _now_ts(),
        ),
    )
    con.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--accession", required=True)
    ap.add_argument("--one-liner", default=None)
    ap.add_argument("--drivers", default="")
    ap.add_argument("--tone", default=None, choices=[None, "up", "down", "neutral", "risk"])
    ap.add_argument("--status", default="ok", choices=["ok", "error", "no_data"])
    ap.add_argument("--error", default=None)
    args = ap.parse_args()

    con = get_stocks_conn()
    try:
        upsert(con, args)
    finally:
        con.close()
    print(f"[ai_filing_summary] upserted {args.symbol}/{args.accession} status={args.status} at={_now_ts()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
