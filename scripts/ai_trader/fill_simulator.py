"""
체결 시뮬레이터 — KIS 현재가 기반 가상 체결.

체결 모델:
  체결가 = 현재가 + 슬리피지 (매수는 위로, 매도는 아래로)
  슬리피지:
    - 시가총액 5조원 이상: 0.15%
    - 1조원 이상: 0.20%
    - 그 외: 0.30%
    (코스피200·코스닥150 멤버십 테이블이 없어 시총 기준 근사)
  수수료: 0.015% (양방향)
  매도세: 0.18% (매도만)

거래 가능 시간: 09:00-18:00 KST (정규장 + 시간외 단일가)
  - 그 외 시간 → tradable=False 반환, 호출자가 거부

CLI 사용 (시뮬만, 체결 안 함):
  python scripts/ai_trader/fill_simulator.py --code 005930 --side buy --qty 10

라이브러리 사용:
  from scripts.ai_trader.fill_simulator import simulate_fill
  result = simulate_fill("005930", "buy", 10)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time
from pathlib import Path
from typing import Literal

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from collectors.kis_api import KISCollector  # noqa: E402

# ── 시뮬레이션 파라미터 ──────────────────────────────────────
COMMISSION_RATE = 0.00015     # 0.015% 양방향
SELL_TAX_RATE = 0.0018        # 0.18% 매도세 (KOSDAQ 0.18%, KOSPI 0.18% — 거래세+농특세 합산)

SLIPPAGE_LARGE = 0.0015       # 0.15% — 시총 5조 이상
SLIPPAGE_MID = 0.0020         # 0.20% — 시총 1조 이상
SLIPPAGE_SMALL = 0.0030       # 0.30% — 그 외

# 거래 가능 시간 (KST) — 정규장 09:00-15:30 + 시간외 단일가 15:40-18:00
TRADABLE_START = time(9, 0)
TRADABLE_END = time(18, 0)


def _slippage_rate(market_cap_won: int) -> float:
    """시가총액 기반 슬리피지율 (코스피200/코스닥150 멤버십 대용)."""
    if market_cap_won <= 0:
        return SLIPPAGE_SMALL
    cap_eok = market_cap_won / 100_000_000  # 원 → 억원
    if cap_eok >= 50_000:   # 5조 이상
        return SLIPPAGE_LARGE
    if cap_eok >= 10_000:   # 1조 이상
        return SLIPPAGE_MID
    return SLIPPAGE_SMALL


def is_tradable_now(now: datetime | None = None) -> tuple[bool, str]:
    """현재 시각이 거래 가능 시간(09:00-18:00 KST)인지."""
    now = now or datetime.now()
    weekday = now.weekday()
    if weekday >= 5:
        return False, f"weekend (weekday={weekday})"
    t = now.time()
    if t < TRADABLE_START or t > TRADABLE_END:
        return False, f"out_of_hours ({t.strftime('%H:%M')} not in 09:00-18:00)"
    # 점심시간 폐지된 지 오래라 거래 가능 — 따로 막지 않음
    return True, "ok"


def simulate_fill(
    code: str,
    side: Literal["buy", "sell"],
    qty: int,
    collector: KISCollector | None = None,
) -> dict:
    """가상 체결 시뮬레이션. 실제 DB 변경은 하지 않음.

    Returns:
        dict with keys:
          ok, reason, code, name, side, qty,
          current_price, fill_price, slippage_pct, slippage_amount,
          gross_amount, commission, tax, net_amount (buy=지출 양수, sell=수입 양수),
          market_cap_won, fetched_at
    """
    side = side.lower().strip()
    if side not in ("buy", "sell"):
        return {"ok": False, "reason": f"invalid_side: {side}"}
    if qty <= 0:
        return {"ok": False, "reason": f"invalid_qty: {qty}"}

    tradable, reason = is_tradable_now()
    if not tradable:
        return {"ok": False, "reason": f"not_tradable: {reason}"}

    c = collector or KISCollector()
    try:
        price_info = c.get_price(code)
    except Exception as e:
        return {"ok": False, "reason": f"kis_price_failed: {e}"}

    current_price = int(price_info.get("current_price") or 0)
    if current_price <= 0:
        return {"ok": False, "reason": "no_price (halted or invalid code?)"}

    market_cap = int(price_info.get("market_cap") or 0)
    # market_cap 단위: KIS hts_avls 는 "억원" 단위. 원으로 환산.
    market_cap_won = market_cap * 100_000_000 if market_cap < 100_000_000 else market_cap
    slip = _slippage_rate(market_cap_won)

    # 슬리피지 적용 — 매수는 위로, 매도는 아래로
    if side == "buy":
        fill_price = round(current_price * (1.0 + slip))
    else:
        fill_price = round(current_price * (1.0 - slip))

    gross = fill_price * qty
    commission = round(gross * COMMISSION_RATE)
    tax = round(gross * SELL_TAX_RATE) if side == "sell" else 0

    # net_amount 의미:
    #   buy  → 계좌에서 빠지는 총액 = gross + commission
    #   sell → 계좌에 들어오는 총액 = gross - commission - tax
    if side == "buy":
        net_amount = gross + commission
    else:
        net_amount = gross - commission - tax

    return {
        "ok": True,
        "reason": "ok",
        "code": code,
        "name": "",  # 호출자가 별도로 채움
        "side": side,
        "qty": qty,
        "current_price": current_price,
        "fill_price": fill_price,
        "slippage_pct": round(slip * 100, 4),
        "slippage_amount": abs(fill_price - current_price) * qty,
        "gross_amount": gross,
        "commission": commission,
        "tax": tax,
        "net_amount": net_amount,
        "market_cap_won": market_cap_won,
        "fetched_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True, help="6자리 종목코드 (예: 005930)")
    ap.add_argument("--side", required=True, choices=["buy", "sell"])
    ap.add_argument("--qty", required=True, type=int)
    ap.add_argument("--json", action="store_true", help="JSON 출력")
    args = ap.parse_args()

    res = simulate_fill(args.code, args.side, args.qty)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 2

    if not res.get("ok"):
        print(f"[fill_simulator] FAIL: {res.get('reason')}")
        return 2

    side_kr = "매수" if res["side"] == "buy" else "매도"
    print(f"[fill_simulator] {args.code} {side_kr} {args.qty}주")
    print(f"  현재가     : {res['current_price']:>12,} 원")
    print(f"  체결가     : {res['fill_price']:>12,} 원 (슬리피지 {res['slippage_pct']}%)")
    print(f"  체결대금   : {res['gross_amount']:>12,} 원")
    print(f"  수수료     : {res['commission']:>12,} 원")
    if res["tax"] > 0:
        print(f"  매도세     : {res['tax']:>12,} 원")
    print(f"  순체결액   : {res['net_amount']:>12,} 원 ({'지출' if res['side']=='buy' else '수입'})")
    print(f"  시가총액   : {res['market_cap_won']:>12,} 원")
    return 0


if __name__ == "__main__":
    sys.exit(main())
