"""
fetch_market_flow.py — KIS 시장 단위 외국인/기관/개인 매매동향을 JSON으로 stdout 출력.

각 brief 루틴(SKILL)이 직전에 호출해 flow_today 필드를 채우는 용도.
신규 테이블/캐시 없음 — 매번 KIS REST 직접 호출.

Usage:
  # 일별 (장 마감 후 신뢰. 어제 데이터는 항상 가능)
  python scripts/fetch_market_flow.py --kind daily --market KOSPI [--date 20260506]
  python scripts/fetch_market_flow.py --kind daily --market KOSDAQ
  python scripts/fetch_market_flow.py --kind daily --market BOTH        # KOSPI+KOSDAQ 묶음

  # 시간대 (장중 누적. 09:00 이후 채워짐)
  python scripts/fetch_market_flow.py --kind time --market KOSPI

출력 구조 (--market KOSPI):
{
  "kind": "daily",
  "market": "KOSPI",
  "date": "2026-05-04",
  "unit": "백만원",
  "foreign_net": -1455933,        # 외국인 순매수 거래대금 (백만원)
  "institution_net": 283792,
  "individual_net": 1182435,
  "foreign_buy": 0, "foreign_sell": 0,
  "institution_buy": 0, "institution_sell": 0,
  "individual_buy": 0, "individual_sell": 0,
  "foreign_net_uk": -14559,       # 억원 정수 (= 백만원/100)
  "institution_net_uk": 2838,
  "individual_net_uk": 11824,
  "index_price": 6598.87,
  "index_change_pct": 0.75,
  "fetched_at": "2026-05-06T08:30:00"
}

--market BOTH 시:
{
  "kind": "daily",
  "fetched_at": "...",
  "KOSPI": { ... },
  "KOSDAQ": { ... }
}

실패 시 stderr 로그 + 해당 market 항목에 {"error": "..."} 박힘 (rt_cd != 0).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


from collectors.kis_api import KISCollector  # noqa: E402

# 시장 코드 매핑
DAILY_ISCD_1 = {"KOSPI": "KSP", "KOSDAQ": "KSQ"}
# 일별 API의 업종코드: 코스피=0001, 코스닥=1001 (코스닥 종합지수: 1001 또는 1002)
# 검증 결과 1001은 rt_cd=9 실패, 1002는 정상 → KOSDAQ는 1002 사용
DAILY_ISCD = {"KOSPI": "0001", "KOSDAQ": "1002"}
# FHPTJ04030000 시간대 API — KIS 가 시장별(001 KOSPI / 002 KOSDAQ) 직접 호출 시
# 응답을 0 으로 반환. 종합(999) 만 정상 (예: 14:00 외국인 +2.3조). 어차피 시장 분리
# 시간대 데이터를 못 받으니 KOSPI/KOSDAQ 둘 다 종합(999) 사용. 응답에 합산임을 명시.
TIME_ISCD = {"KOSPI": "999", "KOSDAQ": "999"}


def _safe_int(v) -> int:
    try:
        if v is None or str(v).strip() in ("", "-"):
            return 0
        return int(float(str(v).replace(",", "")))
    except Exception:
        return 0


def _safe_float(v) -> float:
    try:
        if v is None or str(v).strip() in ("", "-"):
            return 0.0
        return float(str(v).replace(",", ""))
    except Exception:
        return 0.0


def _fetch_daily(c: KISCollector, market: str, date_yyyymmdd: str) -> dict:
    iscd_1 = DAILY_ISCD_1.get(market)
    iscd = DAILY_ISCD.get(market)
    if not iscd_1 or not iscd:
        return {"error": f"unsupported market {market}"}
    res = c._get(
        "/uapi/domestic-stock/v1/quotations/inquire-investor-daily-by-market",
        {
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": iscd,
            "FID_INPUT_DATE_1": date_yyyymmdd,
            "FID_INPUT_ISCD_1": iscd_1,
            "FID_INPUT_DATE_2": date_yyyymmdd,
            "FID_INPUT_ISCD_2": iscd,
        },
        "FHPTJ04040000",
    )
    if res.get("rt_cd") != "0":
        return {"error": res.get("error") or "kis_failed"}

    rows = res.get("output") or []
    if not rows:
        return {"error": "empty_output"}

    # 가장 최근 거래일 = 첫 행이 0(미마감) 이면 두 번째 행 사용
    row = rows[0]
    if _safe_int(row.get("frgn_ntby_tr_pbmn")) == 0 \
       and _safe_int(row.get("orgn_ntby_tr_pbmn")) == 0 \
       and _safe_int(row.get("prsn_ntby_tr_pbmn")) == 0 \
       and len(rows) > 1:
        row = rows[1]

    foreign_net = _safe_int(row.get("frgn_ntby_tr_pbmn"))       # 백만원
    institution_net = _safe_int(row.get("orgn_ntby_tr_pbmn"))
    individual_net = _safe_int(row.get("prsn_ntby_tr_pbmn"))
    raw_date = str(row.get("stck_bsop_date") or date_yyyymmdd)
    iso_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if len(raw_date) == 8 else raw_date

    return {
        "date": iso_date,
        "unit": "백만원",
        "foreign_net": foreign_net,
        "institution_net": institution_net,
        "individual_net": individual_net,
        # 일별 API는 매수/매도 분해 안 줌 — 수량만 줌. 거래대금 분해는 0으로 둠.
        "foreign_buy": 0, "foreign_sell": 0,
        "institution_buy": 0, "institution_sell": 0,
        "individual_buy": 0, "individual_sell": 0,
        # 억원 변환 (백만원 / 100)
        "foreign_net_uk": round(foreign_net / 100),
        "institution_net_uk": round(institution_net / 100),
        "individual_net_uk": round(individual_net / 100),
        "index_price": _safe_float(row.get("bstp_nmix_prpr")),
        "index_change_pct": _safe_float(row.get("bstp_nmix_prdy_ctrt")),
    }


def _fetch_time(c: KISCollector, market: str) -> dict:
    iscd = TIME_ISCD.get(market)
    if not iscd:
        return {"error": f"unsupported market {market}"}
    res = c._get(
        "/uapi/domestic-stock/v1/quotations/inquire-investor-time-by-market",
        {"FID_INPUT_ISCD": iscd, "FID_INPUT_ISCD_2": "S001"},
        "FHPTJ04030000",
    )
    if res.get("rt_cd") != "0":
        return {"error": res.get("error") or "kis_failed"}

    rows = res.get("output") or []
    if not rows:
        return {"error": "empty_output"}

    row = rows[0]  # 가장 최근 시점 누적
    foreign_net = _safe_int(row.get("frgn_ntby_tr_pbmn"))       # 백만원 누적
    institution_net = _safe_int(row.get("orgn_ntby_tr_pbmn"))
    individual_net = _safe_int(row.get("prsn_ntby_tr_pbmn"))

    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "unit": "백만원",
        # 시간대 API 는 KIS 가 종합(999)만 응답. KOSPI+KOSDAQ 합산 값임.
        "scope": "total_kr_market",
        "foreign_net": foreign_net,
        "institution_net": institution_net,
        "individual_net": individual_net,
        "foreign_buy": _safe_int(row.get("frgn_shnu_tr_pbmn")),
        "foreign_sell": _safe_int(row.get("frgn_seln_tr_pbmn")),
        "institution_buy": _safe_int(row.get("orgn_shnu_tr_pbmn")),
        "institution_sell": _safe_int(row.get("orgn_seln_tr_pbmn")),
        "individual_buy": _safe_int(row.get("prsn_shnu_tr_pbmn")),
        "individual_sell": _safe_int(row.get("prsn_seln_tr_pbmn")),
        "foreign_net_uk": round(foreign_net / 100),
        "institution_net_uk": round(institution_net / 100),
        "individual_net_uk": round(individual_net / 100),
        "index_price": 0.0,
        "index_change_pct": 0.0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["daily", "time"], required=True)
    ap.add_argument("--market", choices=["KOSPI", "KOSDAQ", "BOTH"], required=True)
    ap.add_argument("--date", default=None, help="daily 전용. YYYYMMDD. 기본 오늘")
    args = ap.parse_args()

    date_yyyymmdd = args.date or datetime.now().strftime("%Y%m%d")
    fetched_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    c = KISCollector()

    targets = ["KOSPI", "KOSDAQ"] if args.market == "BOTH" else [args.market]

    if args.market == "BOTH":
        out: dict = {"kind": args.kind, "fetched_at": fetched_at}
        for m in targets:
            data = _fetch_daily(c, m, date_yyyymmdd) if args.kind == "daily" else _fetch_time(c, m)
            out[m] = data
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    market = targets[0]
    data = _fetch_daily(c, market, date_yyyymmdd) if args.kind == "daily" else _fetch_time(c, market)
    out = {"kind": args.kind, "market": market, "fetched_at": fetched_at, **data}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if "error" not in data else 2


if __name__ == "__main__":
    sys.exit(main())
