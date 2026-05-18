"""
전 종목에 대한 주린이용 AI 종합 요약을 생성하여 Postgres의 company_ai_summary 테이블에 저장.

사용법:
    python scripts/build_company_ai_summary.py                # 신규/실패만
    python scripts/build_company_ai_summary.py --refresh-all  # 전체 재생성
    python scripts/build_company_ai_summary.py --limit 50     # 우선 50종 테스트
    python scripts/build_company_ai_summary.py --code 321260  # 특정 종목만

진행 상황은 stdout으로, 실패는 status='error'로 DB에 남김 (재시도 대상).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


from collectors.company_overview import CompanyOverviewCollector  # noqa: E402
from scrapers.company_ai_summarizer import CompanyAISummarizer  # noqa: E402
from server.db.connections import get_stocks_conn  # noqa: E402


def _ensure_table(con: Any) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS company_ai_summary (
            code TEXT PRIMARY KEY,
            name TEXT,
            one_liner TEXT,
            business_summary TEXT,
            products TEXT,
            revenue_mix TEXT,
            sector TEXT,
            themes TEXT,
            investor_point TEXT,
            full_summary TEXT,
            raw_facts_json TEXT,
            sources_json TEXT,
            status TEXT,
            error TEXT,
            updated_at TEXT
        )
        """
    )
    try:
        con.commit()
    except Exception:
        pass


def get_targets(con: Any, refresh_all: bool, limit: int | None,
                code: str | None) -> list[tuple[str, str, str | None]]:
    """code, name, seed_oneline 반환. 시총(naver_extended) 큰 순으로 정렬."""
    cur = con.cursor()
    if code:
        rows = cur.execute(
            "SELECT s.code, s.name, cs.summary FROM stocks s "
            "LEFT JOIN company_summary cs ON cs.code = s.code "
            "WHERE s.code = ?", (code,),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    # 우선순위: 시총 큰 종목부터. cap_text가 NULL이거나 매칭 안되는 건 끝으로.
    base_sql = """
        SELECT s.code, s.name, cs.summary, ne.market_cap_text
        FROM stocks s
        LEFT JOIN company_summary cs ON cs.code = s.code
        LEFT JOIN naver_extended ne ON ne.code = s.code
        LEFT JOIN company_ai_summary cas ON cas.code = s.code
    """
    where = []
    if not refresh_all:
        where.append("(cas.code IS NULL OR cas.status != 'ok')")
    if where:
        base_sql += " WHERE " + " AND ".join(where)
    # 한국식 market_cap_text ("44조 5226억", "1,234억", "618000.0" 등)에서
    # 숫자·소수점만 남겨서 NUMERIC 캐스트. 빈 문자열은 NULL 처리.
    base_sql += (
        " ORDER BY CAST(NULLIF(regexp_replace(COALESCE(ne.market_cap_text, '0'), "
        "'[^0-9.]', '', 'g'), '') AS NUMERIC) DESC NULLS LAST"
    )
    rows = cur.execute(base_sql).fetchall()
    out = [(r[0], r[1], r[2]) for r in rows]
    if limit:
        out = out[:limit]
    return out


def get_themes(con: Any, code: str) -> list[str]:
    cur = con.cursor()
    rows = cur.execute("SELECT theme FROM stock_themes WHERE code=?", (code,)).fetchall()
    return [r[0] for r in rows]


def upsert(con: Any, code: str, name: str, summary: dict,
           overview: dict, status: str, error: str | None) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sources = []
    if overview:
        if overview.get("wisereport", {}).get("ok"):
            sources.append("fnguide.wisereport")
        if overview.get("subsidiaries"):
            sources.append("fnguide.svd_corp")
    con.execute(
        """INSERT INTO company_ai_summary
           (code, name, one_liner, business_summary, products, revenue_mix,
            sector, themes, investor_point, full_summary,
            raw_facts_json, sources_json, status, error, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(code) DO UPDATE SET
             name=excluded.name,
             one_liner=excluded.one_liner,
             business_summary=excluded.business_summary,
             products=excluded.products,
             revenue_mix=excluded.revenue_mix,
             sector=excluded.sector,
             themes=excluded.themes,
             investor_point=excluded.investor_point,
             full_summary=excluded.full_summary,
             raw_facts_json=excluded.raw_facts_json,
             sources_json=excluded.sources_json,
             status=excluded.status,
             error=excluded.error,
             updated_at=excluded.updated_at
        """,
        (
            code, name,
            summary.get("one_liner", "") if summary else "",
            summary.get("business_summary", "") if summary else "",
            summary.get("products", "") if summary else "",
            summary.get("revenue_mix", "") if summary else "",
            summary.get("sector", "") if summary else "",
            summary.get("themes", "") if summary else "",
            summary.get("investor_point", "") if summary else "",
            summary.get("full_summary", "") if summary else "",
            json.dumps(overview, ensure_ascii=False) if overview else None,
            json.dumps(sources) if sources else None,
            status, error, now,
        ),
    )
    con.commit()


def process_one(collector: CompanyOverviewCollector,
                summarizer: CompanyAISummarizer,
                con: Any,
                code: str, name: str, seed: str | None) -> tuple[str, str | None]:
    try:
        overview = collector.collect(code)
        wise_ok = (overview.get("wisereport") or {}).get("ok", False)
        themes = get_themes(con, code)
        if not wise_ok and not seed and not themes:
            upsert(con, code, name, {}, overview, "no_data", "no_overview_no_seed")
            return ("no_data", None)

        result = summarizer.summarize(code, name, seed, overview, themes)
        if not result or "_error" in result:
            err = (result or {}).get("_error", "unknown")
            upsert(con, code, name, {}, overview, "error", err)
            return ("error", err)

        upsert(con, code, name, result, overview, "ok", None)
        return ("ok", None)
    except Exception as e:
        tb = traceback.format_exc()[-300:]
        try:
            upsert(con, code, name, {}, {}, "error", f"{type(e).__name__}: {e}")
        except Exception:
            pass
        return ("error", f"{type(e).__name__}: {e}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-all", action="store_true",
                        help="이미 ok 된 종목도 다시 생성")
    parser.add_argument("--limit", type=int, default=None,
                        help="최대 처리 종목 수")
    parser.add_argument("--code", type=str, default=None,
                        help="단일 종목코드만 처리")
    parser.add_argument("--sleep", type=float, default=0.4,
                        help="LLM 호출 사이 sleep 초 (rate limit 보호)")
    args = parser.parse_args()

    con = get_stocks_conn()
    _ensure_table(con)

    targets = get_targets(con, args.refresh_all, args.limit, args.code)
    total = len(targets)
    print(f"[INFO] target stocks: {total}", flush=True)

    collector = CompanyOverviewCollector()
    summarizer = CompanyAISummarizer()

    counts = {"ok": 0, "error": 0, "no_data": 0}
    t0 = time.time()
    for i, (code, name, seed) in enumerate(targets, 1):
        try:
            status, err = process_one(collector, summarizer, con, code, name, seed)
        except KeyboardInterrupt:
            print("[WARN] interrupted by user", flush=True)
            break
        counts[status] = counts.get(status, 0) + 1

        elapsed = time.time() - t0
        rate = i / elapsed if elapsed > 0 else 0
        eta = (total - i) / rate if rate > 0 else 0
        marker = {"ok": "OK", "error": "ER", "no_data": "ND"}[status]
        msg = f"[{i:4d}/{total}] {marker} {code} {name}"
        if err:
            msg += f" :: {err[:80]}"
        msg += f"  | rate={rate:.2f}/s eta={eta/60:.1f}min"
        print(msg, flush=True)

        if status == "ok":
            time.sleep(args.sleep)

    print(f"[DONE] ok={counts['ok']} err={counts['error']} no_data={counts['no_data']} "
          f"elapsed={(time.time()-t0)/60:.1f}min", flush=True)


if __name__ == "__main__":
    main()
