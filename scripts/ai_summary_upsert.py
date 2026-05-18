"""
AI 요약 루틴 **결과를 DB에 기록**.

Claude Code 루틴이 각 타겟에 대해 스스로 요약을 생성한 뒤 이 스크립트로 저장.
AI API를 호출하지 않는다. 발행 시각(updated_at)은 실행 시각으로 자동 기록.

사용법 — 종목:
    python scripts/ai_summary_upsert.py \
      --kind stock --code 000660 \
      --one-liner "외국인 3일 연속 매수에 +3.2% 올라 60일 신고가를 찍었어요." \
      --drivers "외국인 매집,60일 신고가,HBM 뉴스" \
      --tone up \
      --used-signals "price,flow,tech,news"

사용법 — 테마:
    python scripts/ai_summary_upsert.py \
      --kind theme --theme "2차전지" \
      --context "해외 배터리 CAPEX 발표와 LFP 전환 뉴스가 겹치며 +4.2% 상승. 엘앤에프가 +8% 앞장섰어요." \
      --drivers "해외 CAPEX,LFP 전환,엘앤에프 주도" \
      --tone up

실패 기록 (--status error, --error "reason"):
    python scripts/ai_summary_upsert.py --kind stock --code XXXXXX \
      --status error --error "no_data"

규칙:
- status는 ok|error|no_data 중 하나. 생략 시 ok.
- tone은 up|down|neutral|risk 중 하나.
- drivers/used-signals는 콤마 구분 문자열 → 배열로 저장.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


from server.db.connections import get_stocks_conn  # noqa: E402

MODEL_TAG = "claude-code-routine"


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_stock_table(con: Any) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_daily_summary (
            code TEXT NOT NULL,
            summary_date TEXT NOT NULL,
            one_liner TEXT,
            drivers_json TEXT,
            tone TEXT,
            used_signals_json TEXT,
            model TEXT,
            status TEXT,
            error TEXT,
            updated_at TEXT,
            PRIMARY KEY (code, summary_date)
        )
        """
    )


def _ensure_theme_table(con: Any) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS theme_daily_context (
            theme TEXT NOT NULL,
            context_date TEXT NOT NULL,
            context TEXT,
            drivers_json TEXT,
            tone TEXT,
            avg_change_pct REAL,
            stock_count INTEGER,
            model TEXT,
            status TEXT,
            error TEXT,
            updated_at TEXT,
            PRIMARY KEY (theme, context_date)
        )
        """
    )


def _split_csv(s: str | None) -> list[str]:
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


def upsert_stock(con: Any, args) -> None:
    _ensure_stock_table(con)
    con.execute(
        """
        INSERT INTO stock_daily_summary
            (code, summary_date, one_liner, drivers_json, tone, used_signals_json,
             model, status, error, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(code, summary_date) DO UPDATE SET
            one_liner=excluded.one_liner,
            drivers_json=excluded.drivers_json,
            tone=excluded.tone,
            used_signals_json=excluded.used_signals_json,
            model=excluded.model,
            status=excluded.status,
            error=excluded.error,
            updated_at=excluded.updated_at
        """,
        (
            args.code,
            args.date or _today(),
            args.one_liner or "",
            json.dumps(_split_csv(args.drivers), ensure_ascii=False),
            args.tone or "",
            json.dumps(_split_csv(args.used_signals), ensure_ascii=False),
            MODEL_TAG,
            args.status or "ok",
            args.error or None,
            _now_ts(),
        ),
    )
    con.commit()


def upsert_theme(con: Any, args) -> None:
    _ensure_theme_table(con)
    con.execute(
        """
        INSERT INTO theme_daily_context
            (theme, context_date, context, drivers_json, tone,
             avg_change_pct, stock_count, model, status, error, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(theme, context_date) DO UPDATE SET
            context=excluded.context,
            drivers_json=excluded.drivers_json,
            tone=excluded.tone,
            avg_change_pct=excluded.avg_change_pct,
            stock_count=excluded.stock_count,
            model=excluded.model,
            status=excluded.status,
            error=excluded.error,
            updated_at=excluded.updated_at
        """,
        (
            args.theme,
            args.date or _today(),
            args.context or "",
            json.dumps(_split_csv(args.drivers), ensure_ascii=False),
            args.tone or "",
            args.avg_change_pct,
            args.stock_count,
            MODEL_TAG,
            args.status or "ok",
            args.error or None,
            _now_ts(),
        ),
    )
    con.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["stock", "theme"], required=True)
    ap.add_argument("--code", default=None)
    ap.add_argument("--theme", default=None)
    ap.add_argument("--date", default=None, help="기본: 오늘 YYYY-MM-DD")
    ap.add_argument("--status", default="ok", choices=["ok", "error", "no_data"])
    ap.add_argument("--error", default=None)
    ap.add_argument("--tone", default=None, choices=[None, "up", "down", "neutral", "risk"])
    ap.add_argument("--drivers", default="", help="콤마 구분. 예: '외국인 매집,신고가'")

    # stock only
    ap.add_argument("--one-liner", default=None)
    ap.add_argument("--used-signals", default="",
                    help="콤마 구분. 예: 'price,flow,tech,dart,news,short'")

    # theme only
    ap.add_argument("--context", default=None)
    ap.add_argument("--avg-change-pct", type=float, default=None)
    ap.add_argument("--stock-count", type=int, default=None)

    args = ap.parse_args()

    if args.kind == "stock" and not args.code:
        print("--code required for stock", file=sys.stderr)
        return 2
    if args.kind == "theme" and not args.theme:
        print("--theme required for theme", file=sys.stderr)
        return 2

    con = get_stocks_conn()
    try:
        if args.kind == "stock":
            upsert_stock(con, args)
        else:
            upsert_theme(con, args)
    finally:
        con.close()

    print(f"[ai_summary] upserted {args.kind} {args.code or args.theme} "
          f"status={args.status} at={_now_ts()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
