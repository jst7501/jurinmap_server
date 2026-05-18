"""SEC filing subtype 텍스트 기반 재분류.

SEC submissions API 의 primary_doc_desc 는 보통 "424B5" 처럼 짧아서
ATM/Warrant/Reverse Split/Convertible 등 구체 분류가 불가능.
이 스크립트가 filing 본문 텍스트를 fetch_filing_text 로 가져와서
정밀하게 다시 분류 → us_sec_filings.subtype 갱신.

dilution-flagged + 아직 분류 안 된 filing 만 대상 (느린 작업이라 incremental).

사용:
    python scripts/reclassify_us_sec_filings.py --symbols ALP --days 365
    python scripts/reclassify_us_sec_filings.py --penny-only --limit 100
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

from collectors.us_sec_filings import fetch_filing_text  # noqa: E402
from server.db.connections import get_stocks_conn  # noqa: E402

logger = logging.getLogger("scripts.reclassify_us_sec_filings")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def classify_from_text(form: str, text: str, items: str | None) -> str | None:
    """filing 본문 텍스트 기반 정밀 분류.

    primary_doc_desc 만으로는 ATM/Warrant 구분 불가 → 텍스트 정밀 매칭.
    """
    if not text:
        return None
    t = text.lower()
    items_str = (items or "").lower()
    form_u = (form or "").upper()

    # Reverse split — 가장 specific
    if re.search(r"reverse\s+stock\s+split", t) or re.search(r"\d+\s*[-:]\s*for\s*-?\s*\d+\s+reverse", t):
        return "reverse_split"

    # Going concern
    if "going concern" in t and ("substantial doubt" in t or "ability to continue" in t):
        return "going_concern"

    # Delisting / listing notice
    if any(kw in t for kw in ("deficiency letter", "delisting notification", "minimum bid price", "regaining compliance", "listing qualifications", "nasdaq listing rule")):
        return "delisting_risk"
    if "3.01" in items_str:
        return "delisting_risk"

    # ATM offering — 페니의 대표적 무한 dilution 도구
    if re.search(r"at[\s-]the[\s-]market\s+offering", t) or re.search(r"at[\s-]the[\s-]market\s+equity", t):
        return "atm"
    if re.search(r"\batm\s+(offering|program|equity|sales)", t):
        return "atm"
    if "sales agreement" in t and ("at-the-market" in t or "atm" in t):
        return "atm"

    # Registered direct
    if "registered direct offering" in t:
        return "registered_direct"

    # PIPE
    if "private investment in public equity" in t or " pipe transaction" in t:
        return "pipe"
    if "private placement" in t and ("securities purchase agreement" in t or "subscription agreement" in t):
        return "pipe"

    # Convertible
    if re.search(r"convertible\s+(note|debenture|bond|preferred)", t):
        return "convertible"

    # Warrant
    if "warrant" in t and ("exercise price" in t or "purchase warrant" in t or "issued.*warrant" in t):
        return "warrant"
    # 'warrant' 단독은 너무 broad, exercise price 같이 있을 때만

    # Shelf registration (S-3)
    if form_u in ("S-3", "S-3/A", "F-3"):
        if "shelf registration" in t or "may from time to time" in t:
            return "shelf"
        return "shelf"  # S-3 default

    # 그래도 분류 안 되면 form 기반
    if form_u.startswith("424"):
        return "general_offering"
    if form_u in ("S-1", "S-1/A", "F-1"):
        return "general_offering"

    return None


def _select_targets(conn, symbols: list[str], penny_only: bool, days: int, limit: int) -> list[tuple]:
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    where = ["sf.filing_date >= %s", "sf.doc_url IS NOT NULL"]
    params: list = [cutoff]

    # 분류가 없거나 general_offering 인 filing 만 (정밀 재분류)
    where.append("(sf.subtype IS NULL OR sf.subtype IN ('general_offering','shelf'))")
    # dilution-flagged filing 우선
    where.append("sf.is_dilution = TRUE")

    join = ""
    if symbols:
        placeholders = ",".join(["%s"] * len(symbols))
        where.append(f"sf.symbol IN ({placeholders})")
        params.extend(symbols)
    if penny_only:
        join = "INNER JOIN us_stocks us ON us.ticker = sf.symbol AND us.is_penny = TRUE AND (us.is_etf = FALSE OR us.is_etf IS NULL)"

    where_sql = " AND ".join(where)
    sql = f"""
        SELECT sf.symbol, sf.accession, sf.form, sf.doc_url, sf.items
        FROM us_sec_filings sf
        {join}
        WHERE {where_sql}
        ORDER BY sf.filing_date DESC
        LIMIT %s
    """
    params.append(limit)
    cur = conn.execute(sql, tuple(params))
    return cur.fetchall()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="")
    ap.add_argument("--penny-only", action="store_true")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--delay", type=float, default=0.15)
    ap.add_argument("--max-chars", type=int, default=20000)
    args = ap.parse_args()

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    conn = get_stocks_conn()
    try:
        targets = _select_targets(conn, syms, args.penny_only, args.days, args.limit)
        print(f"[targets] {len(targets)}")
        updated = 0
        unchanged = 0
        failed = 0
        for i, (sym, accession, form, doc_url, items) in enumerate(targets):
            try:
                text = fetch_filing_text(doc_url, max_chars=args.max_chars)
                if not text:
                    failed += 1
                    time.sleep(args.delay)
                    continue
                new_subtype = classify_from_text(form, text, items)
                if not new_subtype:
                    unchanged += 1
                    time.sleep(args.delay)
                    continue
                conn.execute(
                    "UPDATE us_sec_filings SET subtype = %s WHERE symbol = %s AND accession = %s",
                    (new_subtype, sym, accession),
                )
                updated += 1
                if (i + 1) % 10 == 0:
                    conn.commit()
                    print(f"  [{i+1}/{len(targets)}] updated={updated} unchanged={unchanged} failed={failed}")
            except Exception as exc:
                logger.debug("reclassify %s/%s: %s", sym, accession, exc)
                failed += 1
            time.sleep(args.delay)
        conn.commit()
        print(f"[done] updated={updated} unchanged={unchanged} failed={failed}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
