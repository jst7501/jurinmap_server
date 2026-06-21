"""
AI 요약 루틴용 **컨텍스트**를 stdout으로 출력.

Claude Code 루틴이 list에서 받은 code/theme마다 이 스크립트를 호출해
요약 입력 자료를 확보한다. AI API를 호출하지 않는다.

사용법:
    python scripts/ai_summary_context.py --kind stock --code 000660
    python scripts/ai_summary_context.py --kind theme --theme "2차전지"

출력: JSON (stdout) — 요약 근거로 쓸 팩트 묶음.
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


def _today_db() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def collect_stock_context(con, code: str) -> dict[str, Any]:
    cur = con.cursor()
    ctx: dict[str, Any] = {"code": code}

    row = cur.execute(
        "SELECT name FROM stocks WHERE code=%s",
        (code,),
    ).fetchone()
    ctx["name"] = row[0] if row else None

    row = cur.execute(
        "SELECT current_price, change_pct, trading_volume, trading_value "
        "FROM price_today WHERE code=%s",
        (code,),
    ).fetchone()
    if row:
        ctx["price"] = {
            "current_price": row[0], "change_pct": row[1],
            "volume": row[2], "trading_value": row[3],
        }

    rows = cur.execute(
        "SELECT date, foreign_net, institution_net, individual_net "
        "FROM investor_flow WHERE code=%s ORDER BY date DESC LIMIT 5",
        (code,),
    ).fetchall()
    ctx["investor_flow_5d"] = [
        {"date": r[0], "foreign": r[1], "institution": r[2], "individual": r[3]}
        for r in rows
    ]

    row = cur.execute(
        "SELECT ma5, ma20, ma60, ma120, div_5, rs_score "
        "FROM tech_analysis WHERE code=%s",
        (code,),
    ).fetchone()
    if row:
        ctx["tech"] = {
            "ma5": row[0], "ma20": row[1], "ma60": row[2], "ma120": row[3],
            "div_5": row[4], "rs_score": row[5],
        }

    # 어제(직전) 한 줄 요약 — 연재 연속성용. 오늘 쓰기 전 어제 이야기를 이어가도록.
    row = cur.execute(
        "SELECT summary_date, one_liner, drivers_json, tone FROM stock_daily_summary "
        "WHERE code=%s AND status='ok' AND summary_date < %s "
        "ORDER BY summary_date DESC LIMIT 1",
        (code, _today_db()),
    ).fetchone()
    if row:
        ctx["prev_summary"] = {
            "date": row[0], "one_liner": row[1],
            "drivers_json": row[2], "tone": row[3],
        }

    # 최근 공시 — 번역·임팩트·강도까지 (강한 공시를 one_liner 에 엮도록)
    rows = cur.execute(
        "SELECT date, title, title_kor, summary_kor, impact, impact_strength "
        "FROM dart_disclosures "
        "WHERE code=%s ORDER BY date DESC LIMIT 5",
        (code,),
    ).fetchall()
    ctx["dart_recent"] = [
        {
            "date": r[0], "title": r[1],
            "title_kor": r[2], "summary_kor": r[3],
            "impact": r[4], "impact_strength": r[5],
        }
        for r in rows
    ]

    today = _today_db()
    rows = cur.execute(
        "SELECT headline FROM news_events "
        "WHERE related_stocks::text LIKE %s AND timestamp >= %s "
        "ORDER BY timestamp DESC LIMIT 5",
        (f"%{code}%", today),
    ).fetchall()
    ctx["news_today"] = [{"title": r[0]} for r in rows]

    row = cur.execute(
        "SELECT short_selling_volume_ratio FROM short_data WHERE code=%s",
        (code,),
    ).fetchone()
    if row:
        ctx["short"] = {"short_ratio": row[0]}

    return ctx


def collect_theme_context(con, theme: str) -> dict[str, Any]:
    cur = con.cursor()
    ctx: dict[str, Any] = {"theme": theme}

    row = cur.execute(
        """
        SELECT AVG(COALESCE(pt.change_pct, 0)) AS avg_pct,
               COUNT(*) AS cnt
        FROM stock_themes st
        LEFT JOIN price_today pt ON pt.code = st.code
        WHERE st.theme=%s
        """,
        (theme,),
    ).fetchone()
    ctx["avg_change_pct"] = float(row[0] or 0) if row else 0.0
    ctx["stock_count"] = int(row[1] or 0) if row else 0

    rows = cur.execute(
        """
        SELECT s.code, s.name, COALESCE(pt.change_pct, 0) AS pct
        FROM stock_themes st
        JOIN stocks s ON s.code = st.code
        LEFT JOIN price_today pt ON pt.code = st.code
        WHERE st.theme=%s
        ORDER BY ABS(COALESCE(pt.change_pct, 0)) DESC
        LIMIT 3
        """,
        (theme,),
    ).fetchall()
    ctx["leaders"] = [
        {"code": r[0], "name": r[1], "change_pct": float(r[2] or 0)}
        for r in rows
    ]

    codes = [lr["code"] for lr in ctx["leaders"]]
    ctx["news"] = []
    # 1) 테마명으로 직접 조회
    rows = cur.execute(
        "SELECT headline FROM news_events "
        "WHERE theme=%s ORDER BY timestamp DESC LIMIT 10",
        (theme,),
    ).fetchall()
    if rows:
        ctx["news"] = [{"title": r[0]} for r in rows]
    elif codes:
        # 2) 리더 종목 코드로 fallback
        rows = cur.execute(
            "SELECT headline FROM news_events "
            "WHERE related_stocks::text LIKE ANY(ARRAY[%s]) "
            "ORDER BY timestamp DESC LIMIT 10",
            ("{" + ",".join(f"%{c}%" for c in codes) + "}",),
        ).fetchall()
        ctx["news"] = [{"title": r[0]} for r in rows]

    return ctx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["stock", "theme"], required=True)
    ap.add_argument("--code", default=None)
    ap.add_argument("--theme", default=None)
    args = ap.parse_args()

    con = get_stocks_conn()
    try:
        if args.kind == "stock":
            if not args.code:
                print("--code required for stock", file=sys.stderr)
                return 2
            data = collect_stock_context(con, args.code)
        else:
            if not args.theme:
                print("--theme required for theme", file=sys.stderr)
                return 2
            data = collect_theme_context(con, args.theme)
    finally:
        con.close()

    json.dump(data, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
