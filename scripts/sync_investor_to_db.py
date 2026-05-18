"""
sync_investor_to_db.py — KIS REST로 전 종목 투자자 매매동향을 Postgres에 채운다.

- investor_flow: 종목×일자별 수급 (PK: (code, date))
- investor_today: 종목별 최신일 요약 (PK: code)

Usage:
  python scripts/sync_investor_to_db.py --mode full --workers 2 --sleep 0.1 --days 20
  python scripts/sync_investor_to_db.py --mode missing --days 5

Design notes:
- FHKST01010900 (inquire-investor) 1회 호출로 최근 약 20거래일 output 배열을 돌려준다.
  따라서 full 모드 1회로 2주치 누락분까지 한번에 메꾼다.
- 서로 다른 worker thread가 동일 KISCollector 인스턴스의 requests.Session을 공유한다.
  requests는 스레드 안전하지만 KIS 레이트 리미터 때문에 기본 workers=2, sleep=0.1로 시작.
- Postgres-only. 실패 시 fallback 없이 raise.
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
from typing import List, Tuple

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)


from collectors.kis_api import KISCollector
from server.db.connections import get_stocks_conn

FAILED_DIR = os.path.join(ROOT_DIR, "logs")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def j(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def calc_individual(row: dict) -> dict:
    """individual 순매수가 0이면 foreign/institution/etc/program 합계로 역산."""
    if row.get("individual", 0) == 0:
        f = row.get("foreign", 0) or 0
        inst = row.get("institution", 0) or 0
        etc = row.get("etc_org", 0) or 0
        prog = row.get("program", 0) or 0
        row["individual"] = -(f + inst + etc + prog)
    return row


def choose_targets(conn, mode: str) -> List[str]:
    """
    full: 전 종목
    missing: investor_today가 없거나 date가 null인 종목 (한 번 채워지면 대상에서 빠짐)
    """
    all_codes = [r[0] for r in conn.execute("SELECT code FROM stocks ORDER BY code").fetchall()]
    if mode == "full":
        return all_codes

    missing = {
        r[0]
        for r in conn.execute(
            "SELECT s.code FROM stocks s LEFT JOIN investor_today t ON t.code=s.code "
            "WHERE t.code IS NULL OR t.date IS NULL"
        ).fetchall()
    }
    return sorted(missing)


def _row_has_trading(r: dict) -> bool:
    """장 개시 전/비거래일 row(수급·거래량 모두 0)는 스킵하기 위한 가드."""
    keys = ("foreign", "institution", "individual",
            "foreign_buy", "foreign_sell",
            "institution_buy", "institution_sell")
    return any((r.get(k) or 0) for k in keys)


def upsert_investor(conn, code: str, rows: List[dict]) -> int:
    """rows = KISCollector.get_investor_history(code) 결과. 최신일이 rows[0]."""
    if not rows:
        return 0

    # 실제 거래 기록이 있는 row만 사용 — 장 개시 전 오늘 row가 전부 0이면 제외.
    valid = [r for r in rows if r.get("date") and r.get("date") != "-" and _row_has_trading(r)]
    if not valid:
        return 0

    # 기관 세분화 컬럼 6종 (bank/insurance/trust/pension/private_fund/etc_finance) —
    # ALTER TABLE IF NOT EXISTS 로 한 번 보장. PG 는 IF NOT EXISTS 지원.
    # idempotent — 이미 있으면 no-op.
    for col in (
        "bank_net", "insurance_net", "trust_net",
        "pension_net", "private_fund_net", "etc_finance_net",
    ):
        try:
            conn.execute(f"ALTER TABLE investor_flow ADD COLUMN IF NOT EXISTS {col} BIGINT")
        except Exception:
            pass

    written = 0
    for r in valid:
        date = r.get("date")
        r = calc_individual(r)
        conn.execute(
            """
            INSERT INTO investor_flow(
              code, date,
              foreign_net, institution_net, individual_net, etc_org_net, program_net,
              foreign_net_amt, institution_net_amt, individual_net_amt,
              foreign_buy, foreign_sell, institution_buy, institution_sell,
              individual_buy, individual_sell,
              foreign_buy_amt, foreign_sell_amt,
              institution_buy_amt, institution_sell_amt,
              individual_buy_amt, individual_sell_amt,
              bank_net, insurance_net, trust_net,
              pension_net, private_fund_net, etc_finance_net
            )
            VALUES(?,?, ?,?,?,?,?, ?,?,?, ?,?,?,?, ?,?, ?,?, ?,?, ?,?, ?,?,?, ?,?,?)
            ON CONFLICT(code, date) DO UPDATE SET
              foreign_net=excluded.foreign_net,
              institution_net=excluded.institution_net,
              individual_net=excluded.individual_net,
              etc_org_net=excluded.etc_org_net,
              program_net=excluded.program_net,
              foreign_net_amt=excluded.foreign_net_amt,
              institution_net_amt=excluded.institution_net_amt,
              individual_net_amt=excluded.individual_net_amt,
              foreign_buy=excluded.foreign_buy,
              foreign_sell=excluded.foreign_sell,
              institution_buy=excluded.institution_buy,
              institution_sell=excluded.institution_sell,
              individual_buy=excluded.individual_buy,
              individual_sell=excluded.individual_sell,
              foreign_buy_amt=excluded.foreign_buy_amt,
              foreign_sell_amt=excluded.foreign_sell_amt,
              institution_buy_amt=excluded.institution_buy_amt,
              institution_sell_amt=excluded.institution_sell_amt,
              individual_buy_amt=excluded.individual_buy_amt,
              individual_sell_amt=excluded.individual_sell_amt,
              bank_net=excluded.bank_net,
              insurance_net=excluded.insurance_net,
              trust_net=excluded.trust_net,
              pension_net=excluded.pension_net,
              private_fund_net=excluded.private_fund_net,
              etc_finance_net=excluded.etc_finance_net
            """,
            (
                code, date,
                r.get("foreign"), r.get("institution"), r.get("individual"),
                r.get("etc_org"), r.get("program"),
                r.get("foreign_net_amt"), r.get("institution_net_amt"), r.get("individual_net_amt"),
                r.get("foreign_buy"), r.get("foreign_sell"),
                r.get("institution_buy"), r.get("institution_sell"),
                r.get("individual_buy"), r.get("individual_sell"),
                r.get("foreign_buy_amt"), r.get("foreign_sell_amt"),
                r.get("institution_buy_amt"), r.get("institution_sell_amt"),
                r.get("individual_buy_amt"), r.get("individual_sell_amt"),
                r.get("bank"), r.get("insurance"), r.get("trust"),
                r.get("pension"), r.get("private_fund"), r.get("etc_finance"),
            ),
        )
        written += 1

    # investor_today: 실제 거래가 반영된 최신 row (장 전이면 전 거래일)
    today_row = calc_individual(dict(valid[0]))
    conn.execute(
        """
        INSERT INTO investor_today(code, date, foreign_net, institution_net, individual_net, full_json)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(code) DO UPDATE SET
          date=excluded.date,
          foreign_net=excluded.foreign_net,
          institution_net=excluded.institution_net,
          individual_net=excluded.individual_net,
          full_json=excluded.full_json
        """,
        (
            code,
            today_row.get("date"),
            today_row.get("foreign"),
            today_row.get("institution"),
            today_row.get("individual"),
            j(today_row),
        ),
    )
    return written


def fetch_one(kis: KISCollector, code: str, max_days: int, sleep_sec: float) -> Tuple[str, list, str]:
    """
    KIS의 inquire-investor는 일시적 레이트 한계에서 exception이 아니라 빈 output을 돌려준다.
    → 빈 응답을 실패로 간주하지 않고 짧은 backoff 후 최대 3회 재시도.
    """
    last_err = ""
    for attempt in range(3):
        try:
            rows = kis.get_investor_history(code, max_days=max_days)
            if rows:
                if sleep_sec > 0:
                    time.sleep(sleep_sec)
                return code, rows, ""
            # 빈 응답: rate-limit 혹은 일시적 장애로 간주하고 backoff
        except Exception as e:
            last_err = str(e)
        time.sleep(0.4 * (attempt + 1))
    return code, [], last_err


def save_failed_rows(rows: List[Tuple[str, str]]) -> str | None:
    if not rows:
        return None
    os.makedirs(FAILED_DIR, exist_ok=True)
    path = os.path.join(
        FAILED_DIR,
        f"investor_sync_failed_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
    )
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["code", "error"])
        for code, err in rows:
            w.writerow([code, err])
    return path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sync investor_flow/today into Postgres (KIS REST)")
    p.add_argument("--mode", choices=["missing", "full"], default="missing")
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--sleep", type=float, default=0.1)
    p.add_argument("--days", type=int, default=20)
    p.add_argument("--batch", type=int, default=100)
    return p.parse_args()


def main():
    args = parse_args()

    print("=" * 72, flush=True)
    print("investor sync start", flush=True)
    print(
        f"mode={args.mode} workers={args.workers} sleep={args.sleep} days={args.days}",
        flush=True,
    )
    print("=" * 72, flush=True)

    conn = get_stocks_conn()
    targets = choose_targets(conn, args.mode)
    conn.close()

    total = len(targets)
    print(f"[targets] {total}", flush=True)
    if not targets:
        print("nothing to do", flush=True)
        return

    kis = KISCollector()

    ok = 0
    fail = 0
    no_row = 0
    failed_rows: List[Tuple[str, str]] = []

    conn = get_stocks_conn()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futures = [
            ex.submit(fetch_one, kis, code, args.days, args.sleep) for code in targets
        ]
        for i, fut in enumerate(as_completed(futures), 1):
            code, rows, err = fut.result()
            if err:
                fail += 1
                failed_rows.append((code, err))
            else:
                ok += 1
                if not rows:
                    no_row += 1
                else:
                    upsert_investor(conn, code, rows)

            if i % max(1, args.batch) == 0 or i == total:
                conn.commit()
                print(
                    f"[{i}/{total}] ok={ok} fail={fail} no_row={no_row}",
                    flush=True,
                )

    conn.commit()
    flow_codes = conn.execute("SELECT COUNT(DISTINCT code) FROM investor_flow").fetchone()[0]
    today_rows = conn.execute("SELECT COUNT(*) FROM investor_today").fetchone()[0]
    max_flow_date = conn.execute("SELECT MAX(date) FROM investor_flow").fetchone()[0]
    max_today_date = conn.execute("SELECT MAX(date) FROM investor_today").fetchone()[0]
    conn.close()

    failed_path = save_failed_rows(failed_rows)

    print("=" * 72, flush=True)
    print("investor sync done", flush=True)
    print(f"ok={ok} fail={fail} no_row={no_row}", flush=True)
    print(
        f"investor_flow distinct_codes={flow_codes} max_date={max_flow_date}",
        flush=True,
    )
    print(
        f"investor_today rows={today_rows} max_date={max_today_date}",
        flush=True,
    )
    if failed_path:
        print(f"failed_log={failed_path}", flush=True)
    print("=" * 72, flush=True)


if __name__ == "__main__":
    main()
