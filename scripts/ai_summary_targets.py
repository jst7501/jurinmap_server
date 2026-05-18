"""
AI 요약 루틴의 **타겟 리스트**를 stdout으로 출력.

Claude Code 루틴이 먼저 이 스크립트를 호출해 "오늘 어느 종목·테마를 요약할지"
확정한다. AI API를 호출하지 않는다.

사용법:
    python scripts/ai_summary_targets.py --kind stock --limit 50
    python scripts/ai_summary_targets.py --kind theme --limit 20
    python scripts/ai_summary_targets.py --kind stock --skip-done   # 오늘 이미 있는 것 제외

출력: JSON 배열 (stdout)
    stock → [{"code": "000660", "name": "SK하이닉스"}, ...]  (거래대금 상위)
    theme → [{"theme": "2차전지", "avg_change_pct": 4.21, "stock_count": 18}, ...]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


from server.db.connections import get_stocks_conn  # noqa: E402


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def list_stock_targets(con, limit: int, skip_done: bool) -> list[dict]:
    cur = con.cursor()
    today = _today()

    sql = """
        SELECT s.code, s.name
        FROM stocks s
        LEFT JOIN price_today pt ON pt.code = s.code
    """
    params: tuple = ()
    if skip_done:
        sql += """
            WHERE NOT EXISTS (
                SELECT 1 FROM stock_daily_summary sds
                WHERE sds.code = s.code
                  AND sds.summary_date = %s
                  AND sds.status = 'ok'
            )
        """
        params = (today,)
    sql += " ORDER BY COALESCE(pt.trading_value, 0) DESC LIMIT %s"
    params = params + (limit,)
    rows = cur.execute(sql, params).fetchall()
    return [{"code": r[0], "name": r[1]} for r in rows]


def list_theme_targets(con, limit: int, skip_done: bool) -> list[dict]:
    cur = con.cursor()
    today = _today()

    # |평균 등락률|이 큰 순 — 가장 "이유 설명이 필요한" 테마
    base = """
        SELECT st.theme,
               AVG(COALESCE(pt.change_pct, 0)) AS avg_pct,
               COUNT(*) AS cnt
        FROM stock_themes st
        LEFT JOIN price_today pt ON pt.code = st.code
    """
    where = ["1=1"]
    params: tuple = ()
    if skip_done:
        where.append("""
            NOT EXISTS (
                SELECT 1 FROM theme_daily_context tdc
                WHERE tdc.theme = st.theme
                  AND tdc.context_date = %s
                  AND tdc.status = 'ok'
            )
        """)
        params = (today,)
    sql = base + " WHERE " + " AND ".join(where) + \
        """
        GROUP BY st.theme
        HAVING COUNT(*) >= 3
        ORDER BY ABS(AVG(COALESCE(pt.change_pct, 0))) DESC
        LIMIT %s
        """
    params = params + (limit,)
    rows = cur.execute(sql, params).fetchall()
    return [
        {"theme": r[0], "avg_change_pct": float(r[1] or 0), "stock_count": int(r[2] or 0)}
        for r in rows
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["stock", "theme"], required=True)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--skip-done", action="store_true",
                    help="오늘 요약이 이미 'ok' 상태인 항목은 제외")
    args = ap.parse_args()

    con = get_stocks_conn()
    try:
        if args.kind == "stock":
            data = list_stock_targets(con, args.limit, args.skip_done)
        else:
            data = list_theme_targets(con, args.limit, args.skip_done)
    finally:
        con.close()

    json.dump(data, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
