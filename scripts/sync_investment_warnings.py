"""
sync_investment_warnings.py
───────────────────────────────────────────────────────────────────
네이버 금융 시장경보 페이지에서 투자주의/경고/위험 종목과 거래정지 종목을
스크래핑해 Postgres `investment_warnings` 테이블에 upsert.

KRX 공식 지정 단계(가벼움 → 무거움):
  - 투자주의(caution):  단기 급등·이상매매 패턴 감지, 경고 전 단계
  - 투자경고(warning):  주의 발령 후 추가 급등/이상 → 하루 매매정지 예고
  - 투자위험(risk):     경고 후에도 지속 → 매매정지·담보비율 상향
  - 거래정지(trading_halt): 실질심사·전자등록 변경·공시 사유로 거래 전면 정지

스키마:
  investment_warnings(code, warning_type, designated_date, reason, note, updated_at)
  PK: (code, warning_type)

Usage:
  python scripts/sync_investment_warnings.py
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from typing import List, Dict, Tuple

import requests
from bs4 import BeautifulSoup

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)


from server.db.connections import get_stocks_conn

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

NAVER_BASE = "https://finance.naver.com"


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_stock_rows(url: str) -> List[Dict]:
    r = requests.get(url, headers=UA, timeout=10)
    r.encoding = "euc-kr"
    soup = BeautifulSoup(r.text, "html.parser")
    tbl = soup.select_one("table.type_2")
    rows: List[Dict] = []
    if not tbl:
        return rows
    for tr in tbl.select("tr"):
        a = tr.find("a", href=re.compile(r"code="))
        if not a:
            continue
        m = re.search(r"code=(\d{6})", a["href"])
        if not m:
            continue
        code = m.group(1)
        name = a.get_text(strip=True)
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        rows.append({"code": code, "name": name, "fields": tds})
    return rows


def fetch_alert(alert_type: str) -> List[Dict]:
    url = f"{NAVER_BASE}/sise/investment_alert.naver?type={alert_type}"
    return _parse_stock_rows(url)


def fetch_trading_halt() -> List[Dict]:
    # 거래정지 종목 리스트 (상장적격성 실질심사, 전자등록 변경 등)
    return _parse_stock_rows(f"{NAVER_BASE}/sise/trading_halt.naver")


def _extract_halt_meta(row: Dict) -> Tuple[str, str]:
    """
    trading_halt 테이블의 tds: [idx, name, 지정일(YYYY.MM.DD), 사유]
    """
    fields = row.get("fields") or []
    designated = ""
    reason = ""
    for f in fields:
        if re.match(r"^\d{4}\.\d{2}\.\d{2}$", f):
            designated = f.replace(".", "")
            break
    if fields:
        # 마지막 텍스트 셀이 사유
        reason = fields[-1]
    return designated, reason


def upsert_warnings(conn, kind: str, rows: List[Dict], ts: str) -> int:
    """kind in {caution, warning, risk, trading_halt}"""
    # 해당 kind의 모든 기존 행을 지우고 최신 리스트로 재구성 (지정 해제된 종목 제거)
    conn.execute(
        "DELETE FROM investment_warnings WHERE warning_type=?", (kind,)
    )
    n = 0
    for r in rows:
        code = r["code"]
        name = r.get("name") or ""
        designated_date = ""
        reason = ""
        note = name  # 종목명 보존 (stocks 테이블과 cross-check용)
        if kind == "trading_halt":
            designated_date, reason = _extract_halt_meta(r)
        conn.execute(
            """
            INSERT INTO investment_warnings(code, warning_type, designated_date, reason, note, updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(code, warning_type) DO UPDATE SET
              designated_date=excluded.designated_date,
              reason=excluded.reason,
              note=excluded.note,
              updated_at=excluded.updated_at
            """,
            (code, kind, designated_date, reason, note, ts),
        )
        n += 1
    return n


def main():
    ts = now_ts()
    print("=" * 72, flush=True)
    print(f"investment_warnings sync start @ {ts}", flush=True)
    print("=" * 72, flush=True)

    conn = get_stocks_conn()
    total = 0
    for kind in ("caution", "warning", "risk"):
        try:
            rows = fetch_alert(kind)
            n = upsert_warnings(conn, kind, rows, ts)
            print(f"  {kind:14s}: {n} stocks", flush=True)
            total += n
        except Exception as e:
            print(f"  {kind}: ERR {type(e).__name__}: {e}", flush=True)

    try:
        halts = fetch_trading_halt()
        n = upsert_warnings(conn, "trading_halt", halts, ts)
        print(f"  trading_halt  : {n} stocks", flush=True)
        total += n
    except Exception as e:
        print(f"  trading_halt: ERR {type(e).__name__}: {e}", flush=True)

    conn.commit()
    by_kind = conn.execute(
        "SELECT warning_type, COUNT(*) FROM investment_warnings GROUP BY warning_type ORDER BY warning_type"
    ).fetchall()
    conn.close()

    print("-" * 72, flush=True)
    for r in by_kind:
        print(f"  total.{r[0]:14s}: {r[1]}", flush=True)
    print(f"total rows upserted: {total}", flush=True)
    print("=" * 72, flush=True)


if __name__ == "__main__":
    main()
