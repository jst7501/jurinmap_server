"""
Sync DART disclosures/shareholder data into Postgres.

- dart_disclosures: recent N disclosures per stock
- dart_shareholders: major shareholder snapshot (majorstock API)

Postgres-only. If the Postgres connection fails this script errors out
loudly rather than falling back to any local file.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Dict, List, Tuple
import xml.etree.ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "server"))


from config.settings import DART_API_KEY
from server.db.connections import get_stocks_conn

BASE = "https://opendart.fss.or.kr/api"
FAILED_DIR = os.path.join(ROOT_DIR, "logs")
CORP_MAP_CACHE = os.path.join(ROOT_DIR, "data", "dart_corp_map.json")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def j(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def new_session(pool: int = 20) -> requests.Session:
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


def safe_get_json(session: requests.Session, url: str, params: dict, timeout: int = 20, tries: int = 4) -> dict:
    last_err = None
    for i in range(tries):
        try:
            r = session.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(0.6 * (i + 1))
    raise last_err


def safe_get_bytes(session: requests.Session, url: str, params: dict, timeout: int = 30, tries: int = 5) -> bytes:
    last_err = None
    for i in range(tries):
        try:
            r = session.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.content
        except Exception as e:
            last_err = e
            time.sleep(0.8 * (i + 1))
    raise last_err


def get_conn():
    return get_stocks_conn()


def load_cached_corp_map() -> Dict[str, str]:
    if not os.path.exists(CORP_MAP_CACHE):
        return {}
    with open(CORP_MAP_CACHE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items() if len(str(k)) == 6 and str(v)}
    return {}


def save_corp_map_cache(data: Dict[str, str]):
    os.makedirs(os.path.dirname(CORP_MAP_CACHE), exist_ok=True)
    with open(CORP_MAP_CACHE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def load_corp_map(prefer_cache_days: int = 7) -> Dict[str, str]:
    """캐시가 최근 N일 이내면 네트워크 스킵 (DART corp_code는 거의 변하지 않음).
    그 외는 API → 실패 시 캐시 fallback.
    """
    if os.path.exists(CORP_MAP_CACHE):
        try:
            age_days = (time.time() - os.path.getmtime(CORP_MAP_CACHE)) / 86400
            if age_days < prefer_cache_days:
                cached = load_cached_corp_map()
                if cached:
                    print(f"[corp_map] using fresh cache ({age_days:.1f}d old): {len(cached)} codes", flush=True)
                    return cached
        except Exception:
            pass

    s = new_session()
    try:
        content = safe_get_bytes(s, f"{BASE}/corpCode.xml", {"crtfc_key": DART_API_KEY}, timeout=30, tries=10)
        out: Dict[str, str] = {}
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            with z.open("CORPCODE.xml") as f:
                root = ET.parse(f).getroot()
                for lst in root.findall("list"):
                    sc = (lst.findtext("stock_code") or "").strip()
                    cc = (lst.findtext("corp_code") or "").strip()
                    if len(sc) == 6 and cc:
                        out[sc] = cc
        if out:
            save_corp_map_cache(out)
        return out
    except Exception:
        cached = load_cached_corp_map()
        if cached:
            print(f"[corp_map] API failed, using cache: {CORP_MAP_CACHE}", flush=True)
            return cached
        raise


def fetch_disclosures(
    session: requests.Session,
    corp_code: str,
    start_date: str,
    end_date: str,
    count: int = 5,
) -> List[dict]:
    data = safe_get_json(
        session,
        f"{BASE}/list.json",
        {
            "crtfc_key": DART_API_KEY,
            "corp_code": corp_code,
            "bgn_de": start_date,
            "end_de": end_date,
            "last_reprt_at": "Y",
            "page_count": str(max(1, min(count, 100))),
        },
        timeout=20,
        tries=4,
    )
    if data.get("status") != "000":
        return []
    rows = data.get("list", []) or []
    return [
        {
            "date": row.get("rcept_dt"),
            "title": row.get("report_nm"),
            "type": row.get("pblntf_ty_nm") or row.get("pblntf_detail_ty"),
        }
        for row in rows[:count]
    ]


def fetch_majorstock(session: requests.Session, corp_code: str) -> dict:
    current_year = datetime.now().year
    for year in (current_year, current_year - 1, current_year - 2, current_year - 3):
        data = safe_get_json(
            session,
            f"{BASE}/majorstock.json",
            {
                "crtfc_key": DART_API_KEY,
                "corp_code": corp_code,
                "bsns_year": str(year),
                "reprt_code": "11011",
            },
            timeout=20,
            tries=4,
        )
        if data.get("status") == "000":
            return {
                "status": "000",
                "message": data.get("message"),
                "year": year,
                "list": data.get("list", []),
            }
    return {"status": "013", "message": "No data", "year": None, "list": []}


def choose_targets(conn, mode: str) -> List[str]:
    all_codes = [r[0] for r in conn.execute("SELECT code FROM stocks ORDER BY code").fetchall()]
    if mode == "full":
        return all_codes

    miss_disc = {
        r[0]
        for r in conn.execute(
            "SELECT s.code FROM stocks s LEFT JOIN dart_disclosures d ON d.code=s.code WHERE d.code IS NULL"
        ).fetchall()
    }
    miss_sh = {
        r[0]
        for r in conn.execute(
            "SELECT s.code FROM stocks s LEFT JOIN dart_shareholders h ON h.code=s.code WHERE h.code IS NULL"
        ).fetchall()
    }
    return sorted(list(miss_disc | miss_sh))


def upsert_dart(conn, code: str, disclosures: List[dict], major: dict, ts: str):
    conn.execute("DELETE FROM dart_disclosures WHERE code=?", (code,))
    for item in disclosures:
        conn.execute(
            "INSERT INTO dart_disclosures(code,date,title,type,created_at) VALUES(?,?,?,?,?)",
            (code, item.get("date"), item.get("title"), item.get("type"), ts),
        )
    conn.execute(
        """
        INSERT INTO dart_shareholders(code,data_json,updated_at)
        VALUES(?,?,?)
        ON CONFLICT(code) DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at
        """,
        (code, j(major), ts),
    )


def fetch_one(
    code: str,
    corp_code: str,
    start_date: str,
    end_date: str,
    disclosure_count: int,
    sleep_sec: float,
) -> Tuple[str, List[dict], dict, str]:
    try:
        session = new_session(pool=4)
        discs = fetch_disclosures(session, corp_code, start_date, end_date, disclosure_count)
        major = fetch_majorstock(session, corp_code)
        if sleep_sec > 0:
            time.sleep(sleep_sec)
        return code, discs, major, ""
    except Exception as e:
        return code, [], {"status": "999", "message": str(e), "year": None, "list": []}, str(e)


def save_failed_rows(rows: List[Tuple[str, str]]) -> str | None:
    if not rows:
        return None
    os.makedirs(FAILED_DIR, exist_ok=True)
    path = os.path.join(FAILED_DIR, f"dart_sync_failed_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["code", "error"])
        for code, err in rows:
            w.writerow([code, err])
    return path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sync DART data into DB")
    p.add_argument("--mode", choices=["missing", "full"], default="missing")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--disclosure-count", type=int, default=5)
    p.add_argument("--batch", type=int, default=120)
    p.add_argument("--sleep", type=float, default=0.03)
    return p.parse_args()


def main():
    args = parse_args()
    if not DART_API_KEY:
        raise RuntimeError("DART_API_KEY is empty.")

    ts = now()
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=max(7, args.days))).strftime("%Y%m%d")

    print("=" * 72)

    print("DART DB sync start")
    print(
        f"mode={args.mode} workers={args.workers} days={args.days} "
        f"disclosure_count={args.disclosure_count} sleep={args.sleep}"
    )
    print("=" * 72)

    corp_map = load_corp_map()
    print(f"[corp_map] {len(corp_map)} codes")

    conn = get_conn()
    targets = choose_targets(conn, args.mode)
    targets = [c for c in targets if c in corp_map]
    print(f"[targets] {len(targets)} (mapped only)")
    conn.close()

    ok = 0
    fail = 0
    no_disc = 0
    no_major = 0
    failed_rows: List[Tuple[str, str]] = []

    conn = get_conn()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futures = [
            ex.submit(fetch_one, code, corp_map[code], start_date, end_date, args.disclosure_count, args.sleep)
            for code in targets
        ]
        total = len(futures)
        for i, fut in enumerate(as_completed(futures), 1):
            code, discs, major, err = fut.result()
            if err:
                fail += 1
                failed_rows.append((code, err))
            else:
                ok += 1
                if not discs:
                    no_disc += 1
                if not (major.get("list") or []):
                    no_major += 1
                upsert_dart(conn, code, discs, major, ts)

            if i % max(1, args.batch) == 0 or i == total:
                conn.commit()
                print(f"[{i}/{total}] ok={ok} fail={fail} no_disc={no_disc} no_major={no_major}")

    conn.commit()
    disc_codes = conn.execute("SELECT COUNT(DISTINCT code) FROM dart_disclosures").fetchone()[0]
    sh_codes = conn.execute("SELECT COUNT(*) FROM dart_shareholders").fetchone()[0]
    conn.close()

    failed_path = save_failed_rows(failed_rows)

    print("=" * 72)
    print("DART DB sync done")
    print(f"ok={ok} fail={fail} no_disc={no_disc} no_major={no_major}")
    print(f"distinct_disclosure_codes={disc_codes} shareholder_codes={sh_codes}")
    if failed_path:
        print(f"failed_log={failed_path}")
    print("=" * 72)


if __name__ == "__main__":
    main()
