"""
stocks ?뚯씠釉붿쓽 ??醫낅ぉ ?꾩옱媛瑜?KIS API濡??쇨큵 媛깆떊?쒕떎.
媛寃?0/NULL) ?ㅽ뙣 醫낅ぉ? ?ъ떆??1???섑뻾.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Tuple

import requests

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "server"))


from config.settings import KIS_APP_KEY, KIS_APP_SECRET, KIS_DOMAIN
from utils.helpers import get_kis_token
from server.db.connections import get_stocks_conn



def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def j(v):
    return json.dumps(v, ensure_ascii=False) if v is not None else None


def safe_float(val, default=0.0):
    try:
        if val is None or str(val).strip() in ("", "nan", "NaN", "-"):
            return default
        return float(str(val).replace(",", ""))
    except Exception:
        return default


def safe_int(val, default=0):
    try:
        return int(safe_float(val, default))
    except Exception:
        return default


def safe_str(val, default=""):
    if val is None or str(val).strip() in ("", "nan", "NaN"):
        return default
    return str(val).strip()


class PriceClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.token = get_kis_token()
        if not self.token:
            self.token = self.issue_token()

    def issue_token(self) -> str:
        url = f"{KIS_DOMAIN}/oauth2/tokenP"
        payload = {
            "grant_type": "client_credentials",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
        }
        r = self.session.post(url, json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"KIS ?좏겙 諛쒓툒 ?ㅽ뙣: {data}")
        return token

    def _headers(self, tr_id: str) -> Dict[str, str]:
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.token}",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def get_price(self, code: str, retry: int = 1) -> dict:
        path = "/uapi/domestic-stock/v1/quotations/inquire-price"
        url = f"{KIS_DOMAIN}{path}"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
        tr_id = "FHKST01010100"

        for attempt in range(retry + 1):
            try:
                r = self.session.get(url, headers=self._headers(tr_id), params=params, timeout=12)
                if r.status_code != 200:
                    return {"ok": False, "error": f"HTTP {r.status_code}", "_raw": {}}
                data = r.json()
                if str(data.get("rt_cd")) != "0":
                    msg = safe_str(data.get("msg1"))
                    if ("?좏겙" in msg or "access token" in msg.lower()) and attempt < retry:
                        self.token = self.issue_token()
                        continue
                    return {"ok": False, "error": msg or "rt_cd!=0", "_raw": data}
                out = data.get("output", {}) or {}
                return {
                    "ok": True if safe_int(out.get("stck_prpr")) > 0 else False,
                    "current_price": safe_int(out.get("stck_prpr")),
                    "change_pct": safe_float(out.get("prdy_ctrt")),
                    "change_amt": safe_int(out.get("prdy_vrss")),
                    "trading_value": safe_int(out.get("acml_tr_pbmn")),
                    "trading_volume": safe_int(out.get("acml_vol")),
                    "volume_turnover_rate": safe_float(out.get("vol_tnrt")),
                    "market_cap": safe_int(out.get("hts_avls")),
                    "per": safe_str(out.get("per")),
                    "pbr": safe_str(out.get("pbr")),
                    "eps": safe_str(out.get("eps")),
                    "foreign_hold_pct": safe_float(out.get("hts_frgn_ehrt")),
                    "listed_shares": safe_int(out.get("lstn_stcn")),
                    "_raw": out,
                    "error": "",
                }
            except Exception as e:
                if attempt >= retry:
                    return {"ok": False, "error": f"{type(e).__name__}: {e}", "_raw": {}}
        return {"ok": False, "error": "unknown", "_raw": {}}


def get_conn():
    return get_stocks_conn()


def upsert_price(conn, code: str, pt: dict, ts: str):
    conn.execute(
        """
        INSERT INTO price_today(
            code,current_price,change_pct,change_amt,trading_value,trading_volume,
            volume_turnover_rate,market_cap,per,pbr,eps,foreign_hold_pct,
            listed_shares,raw_json,updated_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(code) DO UPDATE SET
            current_price=excluded.current_price,
            change_pct=excluded.change_pct,
            change_amt=excluded.change_amt,
            trading_value=excluded.trading_value,
            trading_volume=excluded.trading_volume,
            volume_turnover_rate=excluded.volume_turnover_rate,
            market_cap=excluded.market_cap,
            per=excluded.per,
            pbr=excluded.pbr,
            eps=excluded.eps,
            foreign_hold_pct=excluded.foreign_hold_pct,
            listed_shares=excluded.listed_shares,
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
        """,
        (
            code,
            pt.get("current_price"),
            pt.get("change_pct"),
            pt.get("change_amt"),
            pt.get("trading_value"),
            pt.get("trading_volume"),
            pt.get("volume_turnover_rate"),
            pt.get("market_cap"),
            str(pt.get("per", "")),
            str(pt.get("pbr", "")),
            str(pt.get("eps", "")),
            pt.get("foreign_hold_pct"),
            pt.get("listed_shares"),
            j(pt.get("_raw")),
            ts,
        ),
    )
    conn.execute("UPDATE stocks SET updated_at=? WHERE code=?", (ts, code))


def fetch_one(code: str) -> Tuple[str, dict]:
    client = PriceClient()
    return code, client.get_price(code, retry=1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=14, help="parallel request workers")
    parser.add_argument("--batch", type=int, default=80, help="DB commit interval")
    args = parser.parse_args()

    conn = get_conn()
    codes = [r[0] for r in conn.execute("SELECT code FROM stocks ORDER BY code").fetchall()]
    conn.close()

    stats = {
        "target": len(codes),
        "ok": 0,
        "fail": 0,
    }
    fail_codes: List[str] = []
    start = time.time()

    print("=" * 70)

    print(f"??醫낅ぉ ?꾩옱媛 媛깆떊 ?쒖옉: {len(codes)}媛?(workers={args.workers})")
    print("=" * 70)

    conn = get_conn()
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futures = {ex.submit(fetch_one, code): code for code in codes}
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                _, price = fut.result()
                if price.get("ok") and safe_int(price.get("current_price")) > 0:
                    upsert_price(conn, code, price, now())
                    stats["ok"] += 1
                else:
                    stats["fail"] += 1
                    fail_codes.append(code)
            except Exception:
                stats["fail"] += 1
                fail_codes.append(code)

            done += 1
            if done % max(1, args.batch) == 0 or done == len(codes):
                conn.commit()
                elapsed = time.time() - start
                print(f"  - {done}/{len(codes)} ?꾨즺 | ok={stats['ok']} fail={stats['fail']} | {elapsed:.1f}s")

    conn.commit()
    conn.close()

    # ?ㅽ뙣 醫낅ぉ 1???ъ떆???쒖감, 鍮꾩슜 ??쓬)
    retry_ok = 0
    if fail_codes:
        conn = get_conn()
        client = PriceClient()
        ts = now()
        for idx, code in enumerate(fail_codes, 1):
            p = client.get_price(code, retry=1)
            if p.get("ok") and safe_int(p.get("current_price")) > 0:
                upsert_price(conn, code, p, ts)
                retry_ok += 1
            if idx % 100 == 0 or idx == len(fail_codes):
                conn.commit()
        conn.commit()
        conn.close()

    # 理쒖쥌 ?먭?
    conn = get_conn()
    zero_left = conn.execute(
        "SELECT COUNT(*) FROM price_today WHERE current_price IS NULL OR current_price<=0"
    ).fetchone()[0]
    updated_today = conn.execute(
        "SELECT COUNT(*) FROM price_today WHERE updated_at >= ?",
        (datetime.now().strftime("%Y-%m-%d 00:00:00"),),
    ).fetchone()[0]
    conn.close()

    elapsed = time.time() - start
    print("\n" + "=" * 70)
    print("??醫낅ぉ ?꾩옱媛 媛깆떊 ?꾨즺")
    print(f"?뚯슂: {elapsed:.1f}s")
    print(
        "SUMMARY_JSON="
        + j(
            {
                "target": stats["target"],
                "ok_first_pass": stats["ok"],
                "fail_first_pass": stats["fail"],
                "retry_ok": retry_ok,
                "updated_today_rows": updated_today,
                "zero_or_null_left": zero_left,
            }
        )
    )
    print("=" * 70)


if __name__ == "__main__":
    main()


