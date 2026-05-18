"""
KIS 시장별 투자자매매동향 API smoke test 2 — 단위·필드 전체 확인 + 코스닥 코드 탐색.
"""
import os
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collectors.kis_api import KISCollector  # noqa: E402


def call_realtime(col, iscd, sector, label):
    print(f"\n=== 실시간 [{label}] iscd={iscd} sector={sector} ===")
    res = col._get(
        "/uapi/domestic-stock/v1/quotations/inquire-investor-time-by-market",
        {"FID_INPUT_ISCD": iscd, "FID_INPUT_ISCD_2": sector},
        "FHPTJ04030000",
    )
    if not isinstance(res, dict):
        print(f"  unexpected: {res}")
        return
    print(f"  rt_cd: {res.get('rt_cd')} msg: {res.get('msg1')}")
    out = res.get("output") or []
    if isinstance(out, list) and out:
        r = out[0]
        # 외인/기관/개인 순매수 거래대금 핵심 필드
        for k in ("frgn_ntby_qty", "frgn_ntby_tr_pbmn",
                  "prsn_ntby_qty", "prsn_ntby_tr_pbmn",
                  "orgn_ntby_qty", "orgn_ntby_tr_pbmn",
                  "scrt_ntby_tr_pbmn", "ivtr_ntby_tr_pbmn",
                  "etc_ntby_tr_pbmn"):
            v = r.get(k)
            if v is None:
                continue
            try:
                num = float(v)
                if "tr_pbmn" in k:
                    print(f"    {k}: {v}  (백만원 가정 → {num/100:,.0f}억원)")
                else:
                    print(f"    {k}: {v}")
            except Exception:
                print(f"    {k}: {v}")
    elif isinstance(out, dict):
        print(f"  output(dict) keys: {list(out.keys())}")
    else:
        print(f"  empty output. raw keys: {list(res.keys())}")


def call_daily_full(col, market_iscd, label, date):
    print(f"\n=== 일별 [{label}] {date} ===")
    res = col._get(
        "/uapi/domestic-stock/v1/quotations/inquire-investor-daily-by-market",
        {
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": "0001",
            "FID_INPUT_DATE_1": date,
            "FID_INPUT_ISCD_1": market_iscd,
            "FID_INPUT_DATE_2": date,
            "FID_INPUT_ISCD_2": "0001",
        },
        "FHPTJ04040000",
    )
    out = res.get("output") or []
    if not out:
        print(f"  empty. rt_cd={res.get('rt_cd')} msg={res.get('msg1')}")
        return
    # 4/24 행 찾기
    target = None
    for r in out:
        if r.get("stck_bsop_date") == date:
            target = r
            break
    if not target:
        target = out[0]
    print(f"  date={target.get('stck_bsop_date')}")
    print(f"  field keys ({len(target)}): {list(target.keys())}")
    print()
    # 핵심 수치
    for k in ("frgn_ntby_qty", "frgn_ntby_tr_pbmn",
              "prsn_ntby_qty", "prsn_ntby_tr_pbmn",
              "orgn_ntby_qty", "orgn_ntby_tr_pbmn",
              "scrt_ntby_tr_pbmn", "ivtr_ntby_tr_pbmn"):
        v = target.get(k)
        if v is None:
            continue
        try:
            num = float(v)
            if "tr_pbmn" in k:
                print(f"    {k}: {v}  (백만원 → {num/100:,.0f}억원)")
            else:
                print(f"    {k}: {v}")
        except Exception:
            print(f"    {k}: {v}")


def main():
    col = KISCollector()

    # 1) 일별 코스피 4/24 — 외부 보도와 비교
    call_daily_full(col, "KSP", "코스피", "20260424")
    # 2) 일별 코스닥 4/24
    call_daily_full(col, "KSQ", "코스닥", "20260424")

    # 3) 실시간 — 다양한 sector code 테스트
    for iscd, sector, label in [
        ("999", "S001", "S001=KOSPI 종합?"),
        ("999", "S002", "S002=?"),
        ("999", "K001", "K001=?"),
        ("999", "Q001", "Q001=?"),
        ("999", "0001", "0001 (업종)"),
    ]:
        call_realtime(col, iscd, sector, label)


if __name__ == "__main__":
    main()
