"""US Squeeze Score — 우리가 가진 미국 공매도·차입·소유 데이터 합쳐
"숏 스퀴즈 텐션이 강한 종목" 점수 산출 + Top 랭킹.

데이터 소스 (4개 테이블 JOIN):
  - us_short_volume_daily : 어제 거래 중 공매도 비중 (FINRA Reg SHO Daily)
  - us_short_interest_daily : SI%, Days to Cover (Finviz)
  - us_short_borrow_daily : 차입 수수료, 가능 주식 (iBorrowDesk)
  - us_ownership_daily : 기관·내부자 보유 % (Finviz)

공식 (0~210점):
  short_volume_ratio × 40    : 어제 공매도 비중 (실시간 시그널)
  SI% × 1.5 (cap 30)         : 누적 SI 부담
  DTC × 3 (cap 10)           : 청산 어려움
  CTB × 0.5 (cap 50)         : 차입 비용 부담 (iBorrowDesk fee%)
  +10 if threshold securities list 등재 (강제 buy-in 압력 누적)

높을수록 squeeze 텐션 큼. 100점 넘으면 진짜 후보.
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server.db.connections import get_stocks_conn  # noqa: E402


def compute_scores(min_total_volume: float = 500_000) -> list[dict]:
    """가장 최근 가용 데이터로 squeeze score 계산. 거래량 너무 적은 종목 필터 (noise)."""
    conn = get_stocks_conn()
    try:
        # 1) 최근 short_volume_daily 일자
        cur = conn.execute("SELECT MAX(trade_date) FROM us_short_volume_daily")
        latest_vol_date = cur.fetchone()[0]
        if not latest_vol_date:
            print("[err] us_short_volume_daily 비어있음", file=sys.stderr)
            return []

        # 2) JOIN — short_volume 기준에 다른 테이블의 가장 최근 행을 매칭
        # PG 호환 — LATERAL 대신 sub-select 사용
        sql = """
        WITH sv AS (
            SELECT * FROM us_short_volume_daily WHERE trade_date = %s
        ),
        si AS (
            SELECT DISTINCT ON (symbol) symbol, as_of_date AS si_date,
                   short_float_pct, days_to_cover
            FROM us_short_interest_daily
            ORDER BY symbol, as_of_date DESC
        ),
        bw AS (
            SELECT DISTINCT ON (symbol) symbol, as_of_date AS bw_date,
                   available_shares, borrow_fee_pct, rebate_rate_pct
            FROM us_short_borrow_daily
            ORDER BY symbol, as_of_date DESC
        ),
        ow AS (
            SELECT DISTINCT ON (symbol) symbol, as_of_date AS ow_date,
                   institutional_ownership_pct, insider_ownership_pct
            FROM us_ownership_daily
            ORDER BY symbol, as_of_date DESC
        ),
        th AS (
            SELECT DISTINCT symbol, MAX(as_of_date) AS th_date
            FROM us_threshold_securities_daily
            GROUP BY symbol
        )
        SELECT
            sv.symbol,
            sv.trade_date,
            sv.short_volume_ratio,
            sv.total_volume,
            si.short_float_pct,
            si.days_to_cover,
            bw.borrow_fee_pct,
            bw.available_shares,
            ow.institutional_ownership_pct,
            ow.insider_ownership_pct,
            th.th_date
        FROM sv
        LEFT JOIN si ON si.symbol = sv.symbol
        LEFT JOIN bw ON bw.symbol = sv.symbol
        LEFT JOIN ow ON ow.symbol = sv.symbol
        LEFT JOIN th ON th.symbol = sv.symbol
        WHERE sv.total_volume >= %s
        """
        cur = conn.execute(sql, (latest_vol_date, min_total_volume))
        rows = []
        for r in cur.fetchall():
            (sym, trade_date, svr, total_vol, sip, dtc, ctb, avail, inst_own, ins_own, th_date) = r
            row = {
                "symbol": sym,
                "trade_date": trade_date,
                "short_volume_ratio": float(svr) if svr is not None else 0.0,
                "total_volume": float(total_vol) if total_vol is not None else 0.0,
                "short_float_pct": float(sip) if sip is not None else None,
                "days_to_cover": float(dtc) if dtc is not None else None,
                "borrow_fee_pct": float(ctb) if ctb is not None else None,
                "available_shares": float(avail) if avail is not None else None,
                "institutional_ownership_pct": float(inst_own) if inst_own is not None else None,
                "insider_ownership_pct": float(ins_own) if ins_own is not None else None,
                "threshold_last_date": th_date,
                "is_threshold": th_date is not None,
            }
            # ── Squeeze Score (0~210) ────────────────────────────
            score = 0.0
            score += min(row["short_volume_ratio"], 1.0) * 40        # 0~40
            if row["short_float_pct"] is not None:
                score += min(row["short_float_pct"], 30) * 1.5        # 0~45
            if row["days_to_cover"] is not None:
                score += min(row["days_to_cover"], 10) * 3            # 0~30
            if row["borrow_fee_pct"] is not None:
                score += min(row["borrow_fee_pct"], 50) * 0.5         # 0~25
            if row["is_threshold"]:
                score += 10                                            # threshold bonus
            row["squeeze_score"] = round(score, 1)
            rows.append(row)
        return rows
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20, help="Top N 출력 (default 20)")
    ap.add_argument("--min-volume", type=float, default=500_000,
                    help="최소 일별 거래량 필터 (noise 종목 제거)")
    ap.add_argument("--require-si", action="store_true",
                    help="SI% 데이터 있는 종목만 (더 신뢰도 높은 후보)")
    ap.add_argument("--require-borrow", action="store_true",
                    help="iBorrowDesk 차입 데이터 있는 종목만")
    args = ap.parse_args()

    rows = compute_scores(min_total_volume=args.min_volume)
    if not rows:
        return 1

    if args.require_si:
        rows = [r for r in rows if r["short_float_pct"] is not None]
    if args.require_borrow:
        rows = [r for r in rows if r["borrow_fee_pct"] is not None]

    rows.sort(key=lambda r: r["squeeze_score"], reverse=True)
    top = rows[:args.top]

    # 한국 콘솔 호환 출력 (em-dash 등 회피)
    print(f"\n=== US Squeeze Score TOP {args.top} (trade_date={top[0]['trade_date']}, min_vol={int(args.min_volume):,}) ===")
    print(f"{'symbol':<7} {'score':>6}  {'SV%':>5}  {'SI%':>5}  {'DTC':>5}  {'CTB%':>5}  {'TotalVol':>14}  {'Inst%':>6}  {'Ins%':>6}")
    print("-" * 96)
    for r in top:
        sym = r["symbol"][:6]
        score = r["squeeze_score"]
        svr = f"{r['short_volume_ratio']*100:.1f}"
        sip = f"{r['short_float_pct']:.1f}" if r["short_float_pct"] is not None else "  -"
        dtc = f"{r['days_to_cover']:.2f}" if r["days_to_cover"] is not None else "  -"
        ctb = f"{r['borrow_fee_pct']:.2f}" if r["borrow_fee_pct"] is not None else "  -"
        vol = f"{int(r['total_volume']):>14,}"
        inst = f"{r['institutional_ownership_pct']:.1f}" if r["institutional_ownership_pct"] is not None else "  -"
        ins = f"{r['insider_ownership_pct']:.1f}" if r["insider_ownership_pct"] is not None else "  -"
        print(f"{sym:<7} {score:>6.1f}  {svr:>5}  {sip:>5}  {dtc:>5}  {ctb:>5}  {vol}  {inst:>6}  {ins:>6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
