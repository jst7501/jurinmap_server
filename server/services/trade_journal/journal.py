"""
매매일지 집계 엔진.

회계 방식: **가중평균단가(Weighted Average Cost) 기반 실현손익**

- 같은 타임스탬프 내에서는 BUY를 SELL보다 먼저 처리 → 인트라데이 플립을 정확히 매칭.
- 매수 시: 포지션 수량 증가 + 누적 비용(수수료 포함) 증가 → 평균단가 갱신.
- 매도 시: 실현손익 = (매도가 - 평균단가) × 수량 - 수수료 - 세금.
- 재고를 초과하는 '기간 외 보유분 매도'(FIFO의 취약점)는 해당 수량에 대해
  손익 0(breakeven)으로 처리. 비용은 매도가로 간주.
- 포지션이 0이 되면 '보유 시작일' 리셋.

FIFO 대비 장점
- '기간 외 보유분 매도'를 드롭하지 않음 → 총 거래가 누락되지 않음
- 같은 종목을 여러 번 재진입해도 회계적으로 일관됨
- 인트라데이 플립에서 실제 현금흐름과 정확히 일치
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from server.services.trade_journal.models import Trade


_KOR_DOW = ["월", "화", "수", "목", "금", "토", "일"]


def _key(t: Trade) -> str:
    return t.ticker or t.symbol


def compute_realizations(trades: Iterable[Trade]) -> tuple[list[dict], list[dict]]:
    """
    체결 내역 → (실현 이벤트 리스트, 미청산 포지션 리스트)

    실현 이벤트: 각 SELL 체결마다 1개 생성. 기간 외 보유분이 섞여 있으면 'uncovered' 플래그.
    """
    ordered = sorted(
        trades,
        key=lambda t: (t.traded_at, 0 if t.side == "BUY" else 1, t.id or 0),
    )

    pos: dict[str, dict[str, Any]] = {}
    trips: list[dict] = []

    for t in ordered:
        k = _key(t)
        p = pos.setdefault(k, {
            "qty": 0.0,
            "cost": 0.0,          # 누적 비용 (수수료 포함)
            "first_buy_at": None,
            "symbol": t.symbol,
            "ticker": t.ticker,
        })
        p["symbol"] = t.symbol
        p["ticker"] = t.ticker

        if t.side == "BUY":
            if p["qty"] < 1e-9:
                p["first_buy_at"] = t.traded_at
            p["qty"] += t.quantity
            p["cost"] += t.amount + t.fee
            continue

        # SELL ------------------------------------------------------------
        sell_qty = t.quantity
        if sell_qty <= 0:
            continue

        avg_cost = (p["cost"] / p["qty"]) if p["qty"] > 1e-9 else t.price
        from_inv = min(sell_qty, max(p["qty"], 0.0))
        from_prior = sell_qty - from_inv  # 기간 외 보유분에서 나온 수량

        basis = from_inv * avg_cost + from_prior * t.price  # 비용 귀속
        proceeds = t.amount - t.fee - t.tax
        pnl = proceeds - basis

        first_buy = p["first_buy_at"] or t.traded_at
        holding_days = max((t.traded_at.date() - first_buy.date()).days, 0)

        trips.append({
            "ticker": t.ticker,
            "symbol": t.symbol,
            "qty": sell_qty,
            "buy_price": (basis / sell_qty) if sell_qty else 0.0,
            "sell_price": t.price,
            "buy_amount": basis,
            "sell_amount": t.amount,
            "fees": t.fee,
            "taxes": t.tax,
            "pnl": pnl,
            "return_pct": (pnl / basis * 100.0) if basis > 1e-9 else 0.0,
            "opened_at": first_buy.isoformat(),
            "closed_at": t.traded_at.isoformat(),
            "holding_days": holding_days,
            "uncovered": from_prior > 1e-9,
        })

        # 재고 차감
        if p["qty"] > 0:
            p["cost"] = max(0.0, p["cost"] - from_inv * avg_cost)
            p["qty"] -= from_inv
        if p["qty"] < 1e-9:
            p["qty"] = 0.0
            p["cost"] = 0.0
            p["first_buy_at"] = None

    # 미청산 포지션
    now = datetime.utcnow()
    opens: list[dict] = []
    for k, p in pos.items():
        if p["qty"] > 1e-9:
            first = p["first_buy_at"] or now
            opens.append({
                "ticker": p["ticker"],
                "symbol": p["symbol"],
                "qty": p["qty"],
                "buy_price": p["cost"] / p["qty"] if p["qty"] else 0.0,
                "buy_amount": p["cost"],
                "opened_at": first.isoformat(),
                "days_held": (now.date() - first.date()).days,
            })
    return trips, opens


# 이름 호환 유지 (main.py가 import 하는 이름)
compute_roundtrips = compute_realizations


def build_journal_tree(trades: list[Trade]) -> dict[str, Any]:
    """월 → 일 → 실현이벤트 계층 트리 + 전체 요약."""
    trips, opens = compute_realizations(trades)

    months: dict[str, dict] = {}
    for rt in trips:
        closed = datetime.fromisoformat(rt["closed_at"])
        mkey = f"{closed.year}-{closed.month:02d}"
        dkey = closed.date().isoformat()

        m = months.setdefault(mkey, {
            "key": mkey,
            "label": f"{closed.year}년 {closed.month}월",
            "pnl": 0.0,
            "trip_count": 0,
            "win_count": 0,
            "loss_count": 0,
            "_days": {},
        })
        m["pnl"] += rt["pnl"]
        m["trip_count"] += 1
        if rt["pnl"] > 0:
            m["win_count"] += 1
        elif rt["pnl"] < 0:
            m["loss_count"] += 1

        d = m["_days"].setdefault(dkey, {
            "key": dkey,
            "label": f"{closed.month}.{closed.day:02d} ({_KOR_DOW[closed.weekday()]})",
            "pnl": 0.0,
            "trip_count": 0,
            "trips": [],
        })
        d["pnl"] += rt["pnl"]
        d["trip_count"] += 1
        d["trips"].append(rt)

    month_list: list[dict] = []
    for mkey in sorted(months.keys(), reverse=True):
        m = months[mkey]
        days = list(m.pop("_days").values())
        days.sort(key=lambda d: d["key"], reverse=True)
        for d in days:
            d["trips"].sort(key=lambda r: abs(r["pnl"]), reverse=True)
        m["days"] = days
        month_list.append(m)

    total_pnl = sum(r["pnl"] for r in trips)
    total_win = sum(1 for r in trips if r["pnl"] > 0)
    total_loss = sum(1 for r in trips if r["pnl"] < 0)
    win_rate = (total_win / (total_win + total_loss) * 100.0) if (total_win + total_loss) else 0.0
    best = max(trips, key=lambda r: r["pnl"], default=None)
    worst = min(trips, key=lambda r: r["pnl"], default=None)

    return {
        "total_pnl": total_pnl,
        "trip_count": len(trips),
        "win_count": total_win,
        "loss_count": total_loss,
        "win_rate": win_rate,
        "best_trip": best,
        "worst_trip": worst,
        "open_position_count": len(opens),
        "months": month_list,
    }
