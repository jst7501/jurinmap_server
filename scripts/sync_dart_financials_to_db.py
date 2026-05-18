"""
sync_dart_financials_to_db.py — DART fnlttSinglAcntAll 기반 3년치 핵심 재무 수집

DART 공식 API /api/fnlttSinglAcntAll.json 의 사업보고서(reprt_code=11011) 응답은
당기(thstrm_amount) / 전기(frmtrm_amount) / 전전기(bfefrmtrm_amount) 3년치
금액을 한 번에 돌려준다. 여기서 '매출액 / 영업이익 / 당기순이익' 3개 핵심
계정만 뽑아 Postgres `finance_statements` 테이블에 upsert한다.

저장 스키마:
  finance_statements(code, year, account_nm, amount, fs_div, currency, updated_at)
  PK: (code, year, account_nm)

Usage:
  python scripts/sync_dart_financials_to_db.py --mode missing --workers 4
  python scripts/sync_dart_financials_to_db.py --mode full --workers 4
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)


from config.settings import DART_API_KEY
from server.db.connections import get_stocks_conn

BASE = "https://opendart.fss.or.kr/api"
CORP_MAP_CACHE = os.path.join(ROOT_DIR, "data", "dart_corp_map.json")
FAILED_DIR = os.path.join(ROOT_DIR, "logs")

# 프론트가 쓰는 3개 핵심 계정. DART 응답의 account_nm 는 "영업이익(손실)",
# "당기순이익(손실)" 같은 변형이 많아 정규화 필수.
TARGET_ACCOUNTS = {"영업이익", "매출액", "당기순이익"}
ALIASES: Dict[str, str] = {
    # 영업이익 변형
    "영업이익(손실)": "영업이익",
    "영업손실": "영업이익",
    "계속영업이익": "영업이익",
    "계속영업이익(손실)": "영업이익",
    # 매출액 변형
    "수익(매출액)": "매출액",
    "영업수익": "매출액",
    "매출": "매출액",
    "매출수익": "매출액",
    # 당기순이익 변형
    "당기순이익(손실)": "당기순이익",
    "당기순손실": "당기순이익",
    "반기순이익": "당기순이익",
    "반기순이익(손실)": "당기순이익",
    "분기순이익": "당기순이익",
    "분기순이익(손실)": "당기순이익",
    "연결당기순이익": "당기순이익",
    "연결당기순이익(손실)": "당기순이익",
    "법인세차감후기순이익": "당기순이익",
    "법인세비용차감후계속사업순이익": "당기순이익",
    "지배기업의 소유주에게 귀속되는 당기순이익": "당기순이익",
    "지배기업의 소유주에게 귀속되는 당기순이익(손실)": "당기순이익",
    "지배기업 소유주 귀속 당기순이익": "당기순이익",
    "지배기업 소유주 귀속 당기순이익(손실)": "당기순이익",
}


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_session(pool: int = 4) -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    retries = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=pool, pool_maxsize=pool)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def safe_get_json(sess: requests.Session, url: str, params: dict, timeout: int = 20, tries: int = 4) -> dict:
    last_err = None
    for i in range(tries):
        try:
            r = sess.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(0.6 * (i + 1))
    raise last_err


def load_corp_map() -> Dict[str, str]:
    with open(CORP_MAP_CACHE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {str(k): str(v) for k, v in data.items() if len(str(k)) == 6 and str(v)}


def parse_amount(value) -> int | None:
    """DART는 '43,376,630' 또는 '(1,234)' 음수, 빈 문자열/'-' 반환. None or int."""
    if value is None:
        return None
    t = str(value).replace(",", "").replace(" ", "").strip()
    if t in ("", "-", "None"):
        return None
    if t.startswith("(") and t.endswith(")"):
        t = "-" + t[1:-1]
    try:
        return int(t)
    except Exception:
        try:
            return int(float(t))
        except Exception:
            return None


def fetch_financials(sess: requests.Session, corp_code: str, bsns_year: int):
    """CFS(연결) 우선, 없으면 OFS(별도). 성공 payload 또는 None."""
    for fs_div in ("CFS", "OFS"):
        try:
            data = safe_get_json(
                sess,
                f"{BASE}/fnlttSinglAcntAll.json",
                {
                    "crtfc_key": DART_API_KEY,
                    "corp_code": corp_code,
                    "bsns_year": str(bsns_year),
                    "reprt_code": "11011",
                    "fs_div": fs_div,
                },
                timeout=20,
                tries=3,
            )
        except Exception:
            continue
        if data.get("status") == "000" and data.get("list"):
            return {"list": data["list"], "fs_div": fs_div, "bsns_year": int(bsns_year)}
    return None


def extract_three_years(payload: dict) -> List[Tuple[int, str, int, str]]:
    """payload -> [(year, account_nm, amount, fs_div), ...] — 최신부터 전전기까지."""
    if not payload:
        return []
    bsns_year = payload["bsns_year"]
    fs_div = payload["fs_div"]
    rows: List[Tuple[int, str, int, str]] = []
    seen: set = set()  # (year, account) 중복 방지 (IS / CIS 겹치는 경우 첫 매치 유지)

    for item in payload["list"]:
        raw_nm = (item.get("account_nm") or "").strip()
        nm = ALIASES.get(raw_nm, raw_nm)
        if nm not in TARGET_ACCOUNTS:
            continue
        thst = parse_amount(item.get("thstrm_amount"))
        frm = parse_amount(item.get("frmtrm_amount"))
        bfe = parse_amount(item.get("bfefrmtrm_amount"))
        for year, amt in ((bsns_year, thst), (bsns_year - 1, frm), (bsns_year - 2, bfe)):
            if amt is None:
                continue
            key = (year, nm)
            if key in seen:
                continue
            seen.add(key)
            rows.append((year, nm, amt, fs_div))
    return rows


def ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS finance_statements (
            code        TEXT NOT NULL,
            year        INTEGER NOT NULL,
            account_nm  TEXT NOT NULL,
            amount      BIGINT,
            fs_div      TEXT,
            currency    TEXT,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (code, year, account_nm)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fs_code ON finance_statements(code)")
    conn.commit()


def upsert_financials(conn, code: str, rows: List[Tuple[int, str, int, str]], ts: str) -> int:
    n = 0
    for year, account, amount, fs_div in rows:
        conn.execute(
            """
            INSERT INTO finance_statements(code, year, account_nm, amount, fs_div, currency, updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(code, year, account_nm) DO UPDATE SET
              amount=excluded.amount,
              fs_div=excluded.fs_div,
              currency=excluded.currency,
              updated_at=excluded.updated_at
            """,
            (code, int(year), account, int(amount), fs_div, "KRW", ts),
        )
        n += 1
    return n


def choose_targets(conn, mode: str) -> List[str]:
    all_codes = [r[0] for r in conn.execute("SELECT code FROM stocks ORDER BY code").fetchall()]
    if mode == "full":
        return all_codes
    missing = [
        r[0]
        for r in conn.execute(
            "SELECT s.code FROM stocks s "
            "LEFT JOIN finance_statements f ON f.code=s.code "
            "WHERE f.code IS NULL"
        ).fetchall()
    ]
    return sorted(missing)


def fetch_one(code: str, corp_code: str, year_candidates: List[int], sleep_sec: float) -> Tuple[str, list, str]:
    try:
        sess = new_session(pool=2)
        for y in year_candidates:
            payload = fetch_financials(sess, corp_code, y)
            if payload:
                rows = extract_three_years(payload)
                if sleep_sec > 0:
                    time.sleep(sleep_sec)
                return code, rows, ""
        return code, [], ""
    except Exception as e:
        return code, [], str(e)


def save_failed(rows: List[Tuple[str, str]]) -> str | None:
    if not rows:
        return None
    os.makedirs(FAILED_DIR, exist_ok=True)
    p = os.path.join(
        FAILED_DIR,
        f"dart_financials_failed_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
    )
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["code", "error"])
        for c, e in rows:
            w.writerow([c, e])
    return p


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sync DART financial statements (3-year core accounts)")
    p.add_argument("--mode", choices=["missing", "full"], default="missing")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--sleep", type=float, default=0.05)
    p.add_argument("--batch", type=int, default=100)
    return p.parse_args()


def main():
    args = parse_args()
    if not DART_API_KEY:
        raise RuntimeError("DART_API_KEY is empty.")

    print("=" * 72, flush=True)
    print("DART financials sync start", flush=True)
    print(f"mode={args.mode} workers={args.workers} sleep={args.sleep}", flush=True)
    print("=" * 72, flush=True)

    current_year = datetime.now().year
    # 사업보고서는 다음해 3-4월 공시 → current_year-1 부터 역순 시도.
    year_candidates = [current_year - 1, current_year - 2, current_year - 3]

    corp_map = load_corp_map()
    print(f"[corp_map] {len(corp_map)} codes", flush=True)

    conn = get_stocks_conn()
    ensure_table(conn)
    targets_all = choose_targets(conn, args.mode)
    targets = [c for c in targets_all if c in corp_map]
    print(f"[targets] {len(targets)} mapped (of {len(targets_all)})", flush=True)
    conn.close()

    if not targets:
        print("nothing to do", flush=True)
        return

    ok = 0
    fail = 0
    no_data = 0
    failed_rows: List[Tuple[str, str]] = []
    ts = now_ts()

    conn = get_stocks_conn()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futures = [
            ex.submit(fetch_one, c, corp_map[c], year_candidates, args.sleep) for c in targets
        ]
        total = len(futures)
        for i, fut in enumerate(as_completed(futures), 1):
            code, rows, err = fut.result()
            if err:
                fail += 1
                failed_rows.append((code, err))
            else:
                ok += 1
                if not rows:
                    no_data += 1
                else:
                    upsert_financials(conn, code, rows, ts)
            if i % max(1, args.batch) == 0 or i == total:
                conn.commit()
                print(f"[{i}/{total}] ok={ok} fail={fail} no_data={no_data}", flush=True)

    conn.commit()
    codes_with_data = conn.execute("SELECT COUNT(DISTINCT code) FROM finance_statements").fetchone()[0]
    total_rows = conn.execute("SELECT COUNT(*) FROM finance_statements").fetchone()[0]
    conn.close()

    fp = save_failed(failed_rows)
    print("=" * 72, flush=True)
    print(f"DONE ok={ok} fail={fail} no_data={no_data}", flush=True)
    print(
        f"finance_statements distinct_codes={codes_with_data} rows={total_rows}",
        flush=True,
    )
    if fp:
        print(f"failed_log={fp}", flush=True)
    print("=" * 72, flush=True)


if __name__ == "__main__":
    main()
