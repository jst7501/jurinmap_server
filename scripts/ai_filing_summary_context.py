"""
SEC 8-K / 6-K filing **본문 + 메타**를 stdout으로 출력 (JSON).

Claude Code 루틴이 이 컨텍스트를 받아 한 줄 한국어 요약을 만든다.

사용법:
    python scripts/ai_filing_summary_context.py --symbol ALP --accession 0001234567-26-000001

출력:
    {
      "symbol": "ALP", "accession": "...", "form": "8-K",
      "filing_date": "2026-05-14", "items": "1.01,8.01",
      "primary_doc_desc": "...", "doc_url": "https://...",
      "text": "(filing 본문, max 30000자)",
      "company": {"entity_name": "Alpha Compute", "is_penny": true, "market_cap_usd": ...}
    }
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.db.connections import get_stocks_conn  # noqa: E402
from collectors.us_sec_filings import fetch_filing_text  # noqa: E402


# 8-K Item 코드 풀이 (Claude Code 가 요약 시 참고)
ITEM_LABELS = {
    "1.01": "물질적 계약 체결",
    "1.02": "물질적 계약 해지",
    "1.03": "파산 또는 회생",
    "2.01": "인수·합병·자산처분 완료",
    "2.02": "실적 발표",
    "2.03": "물질적 직접 재무 의무",
    "2.04": "off-balance 약정",
    "2.05": "구조조정 비용",
    "2.06": "물질적 손상차손",
    "3.01": "상장폐지 통지",
    "3.02": "주식 비공모 발행",
    "3.03": "주주 권리 변경",
    "4.01": "회계법인 변경",
    "4.02": "이전 재무제표 의존 금지",
    "5.01": "지배권 변경",
    "5.02": "임원 변경 (CEO·CFO·이사회)",
    "5.03": "정관·내규 변경",
    "5.07": "주주총회 표결 결과",
    "5.08": "주주제안",
    "7.01": "Reg FD 공시",
    "8.01": "기타 중요 사건",
    "9.01": "재무제표 및 첨부서류",
}


def expand_items(items_csv: str | None) -> list[dict]:
    if not items_csv:
        return []
    out = []
    for it in items_csv.split(","):
        it = it.strip()
        if it:
            out.append({"code": it, "label_ko": ITEM_LABELS.get(it, "기타")})
    return out


def get_filing(con, symbol: str, accession: str) -> dict | None:
    cur = con.cursor()
    sql = """
        SELECT sf.symbol, sf.accession, sf.form, sf.filing_date,
               sf.primary_doc, sf.primary_doc_desc, sf.items, sf.doc_url,
               us.name, us.name_ko, us.is_penny, us.market_cap_usd,
               us.sector, us.industry
        FROM us_sec_filings sf
        LEFT JOIN us_stocks us ON us.ticker = sf.symbol
        WHERE sf.symbol = %s AND sf.accession = %s
    """
    r = cur.execute(sql, (symbol.upper(), accession)).fetchone()
    if not r:
        return None
    return {
        "symbol": r[0],
        "accession": r[1],
        "form": r[2],
        "filing_date": r[3].isoformat() if r[3] and hasattr(r[3], "isoformat") else str(r[3] or ""),
        "primary_doc": r[4],
        "primary_doc_desc": r[5],
        "items": r[6],
        "items_expanded": expand_items(r[6]),
        "doc_url": r[7],
        "company": {
            "name": r[8],
            "name_ko": r[9],
            "is_penny": bool(r[10]),
            "market_cap_usd": float(r[11]) if r[11] is not None else None,
            "sector": r[12],
            "industry": r[13],
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--accession", required=True)
    ap.add_argument("--max-chars", type=int, default=20000)
    ap.add_argument("--no-text", action="store_true", help="filing 본문 안 가져옴 (메타만)")
    args = ap.parse_args()

    con = get_stocks_conn()
    try:
        meta = get_filing(con, args.symbol, args.accession)
    finally:
        con.close()
    if not meta:
        print(json.dumps({"error": "filing not found"}), file=sys.stderr)
        return 2

    text = None
    if not args.no_text and meta.get("doc_url"):
        text = fetch_filing_text(meta["doc_url"], max_chars=args.max_chars)
    meta["text"] = text

    json.dump(meta, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
