"""
종토방 민심 분석 루틴 — **타겟 리스트** 출력.

Claude Code 루틴이 먼저 이 스크립트를 호출해 "오늘 어느 종목의 종토방을 분석할지"
확정. 외부 AI API 없음.

사용법:
    python scripts/board_targets.py --limit 100
    python scripts/board_targets.py --limit 100 --skip-done   # 오늘 이미 분석된 건 제외

출력: JSON 배열 (stdout)
  [{"code":"000660","name":"SK하이닉스","market":"KOSPI","trading_value":12345000000}, ...]
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


def list_board_targets(conn, limit: int, skip_done: bool) -> list[dict]:
    """거래대금 상위 N 종목. skip_done 이면 오늘 ai_analysis ok 인 건 제외."""
    cur = conn.cursor()
    today = _today()

    # ai_analysis.updated_at 이 오늘 날짜로 시작하면 완료로 판단
    sql = """
        SELECT s.code, s.name, COALESCE(s.market, '') AS market,
               COALESCE(pt.trading_value, 0) AS trading_value
        FROM stocks s
        JOIN price_today pt ON pt.code = s.code
        WHERE COALESCE(pt.trading_value, 0) > 0
    """
    params = []
    if skip_done:
        sql += """
            AND s.code NOT IN (
                SELECT code FROM ai_analysis
                WHERE updated_at::text LIKE %s
            )
        """
        params.append(today + "%")
    sql += " ORDER BY pt.trading_value DESC LIMIT %s"
    params.append(limit)

    cur.execute(sql, tuple(params))
    rows = cur.fetchall()
    out = []
    for r in rows:
        code = str(r[0]).zfill(6)
        out.append({
            "code": code,
            "name": r[1] or code,
            "market": r[2] or "UNKNOWN",
            "trading_value": int(r[3] or 0),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--skip-done", action="store_true",
                    help="오늘 이미 ai_analysis 에 업데이트된 코드는 제외")
    args = ap.parse_args()

    conn = get_stocks_conn()
    try:
        targets = list_board_targets(conn, args.limit, args.skip_done)
    finally:
        conn.close()

    print(json.dumps(targets, ensure_ascii=False, indent=None))


if __name__ == "__main__":
    main()
