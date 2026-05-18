"""
SEC 8-K / 6-K AI 요약 루틴 — **타겟 리스트** 출력 (stdout, JSON 배열).

Claude Code 루틴이 먼저 이 스크립트로 "어느 filing 을 요약할지" 결정.
AI API 호출 X.

사용법:
    python scripts/ai_filing_summary_targets.py --limit 30 --skip-done
    python scripts/ai_filing_summary_targets.py --forms 8-K,6-K --limit 50 --days 7

출력:
    [
      {"symbol": "ALP", "accession": "0001234567-26-000001",
       "form": "8-K", "filing_date": "2026-05-14",
       "primary_doc_desc": "...", "items": "1.01,8.01",
       "doc_url": "https://..."},
      ...
    ]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.db.connections import get_stocks_conn  # noqa: E402


def list_targets(con, forms: list[str], limit: int, days: int, skip_done: bool, penny_only: bool) -> list[dict]:
    cur = con.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

    where = ["sf.is_summary_target = TRUE", "sf.filing_date >= %s"]
    params: list = [cutoff]

    if forms:
        placeholders = ",".join(["%s"] * len(forms))
        where.append(f"sf.form IN ({placeholders})")
        params.extend(forms)

    join_penny = ""
    if penny_only:
        join_penny = "INNER JOIN us_stocks us ON us.ticker = sf.symbol AND us.is_penny = TRUE AND (us.is_etf = FALSE OR us.is_etf IS NULL)"

    skip_join = ""
    if skip_done:
        skip_join = """
            LEFT JOIN us_filing_summaries fs ON fs.symbol = sf.symbol AND fs.accession = sf.accession AND fs.status = 'ok'
        """
        where.append("fs.accession IS NULL")

    where_sql = " AND ".join(where)

    sql = f"""
        SELECT sf.symbol, sf.accession, sf.form, sf.filing_date,
               sf.primary_doc, sf.primary_doc_desc, sf.items, sf.doc_url
        FROM us_sec_filings sf
        {join_penny}
        {skip_join}
        WHERE {where_sql}
        ORDER BY sf.filing_date DESC, sf.symbol ASC
        LIMIT %s
    """
    params.append(limit)
    rows = cur.execute(sql, tuple(params)).fetchall()
    return [
        {
            "symbol": r[0],
            "accession": r[1],
            "form": r[2],
            "filing_date": r[3].isoformat() if r[3] and hasattr(r[3], "isoformat") else str(r[3] or ""),
            "primary_doc": r[4],
            "primary_doc_desc": r[5],
            "items": r[6],
            "doc_url": r[7],
        }
        for r in rows
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forms", default="8-K,6-K", help="콤마 구분")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--skip-done", action="store_true")
    ap.add_argument("--penny-only", action="store_true")
    args = ap.parse_args()

    forms = [f.strip() for f in args.forms.split(",") if f.strip()]
    con = get_stocks_conn()
    try:
        data = list_targets(con, forms, args.limit, args.days, args.skip_done, args.penny_only)
    finally:
        con.close()

    json.dump(data, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
