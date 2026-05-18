from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlmodel import Session, select

from server.services.trade_journal.models import Trade


@dataclass(frozen=True)
class TradeSummary:
    count: int
    total_buy: float
    total_sell: float
    total_fee: float
    total_tax: float
    realized_pnl: float

    def as_dict(self) -> dict:
        return {
            "count": self.count,
            "total_buy": self.total_buy,
            "total_sell": self.total_sell,
            "total_fee": self.total_fee,
            "total_tax": self.total_tax,
            "realized_pnl": self.realized_pnl,
        }

    def digest(self) -> tuple:
        return (
            int(self.count),
            round(float(self.total_buy), 2),
            round(float(self.total_sell), 2),
            round(float(self.total_fee), 2),
            round(float(self.total_tax), 2),
            round(float(self.realized_pnl), 2),
        )


def compute_summary_from_trades(trades: Iterable[Trade]) -> TradeSummary:
    trades = list(trades)
    total_buy = sum(t.amount for t in trades if t.side == "BUY")
    total_sell = sum(t.amount for t in trades if t.side == "SELL")
    total_fee = sum(t.fee for t in trades)
    total_tax = sum(t.tax for t in trades)
    realized = total_sell - total_buy - total_fee - total_tax
    return TradeSummary(
        count=len(trades),
        total_buy=total_buy,
        total_sell=total_sell,
        total_fee=total_fee,
        total_tax=total_tax,
        realized_pnl=realized,
    )


def compute_summary(session: Session) -> TradeSummary:
    trades = session.exec(select(Trade)).all()
    return compute_summary_from_trades(trades)


def replace_all_trades(session: Session, trades: list[Trade]) -> tuple[int, int]:
    removed = session.query(Trade).delete()
    session.commit()

    for t in trades:
        session.add(t)
    session.commit()
    return removed, len(trades)

