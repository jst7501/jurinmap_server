"""
ai_trader 현재 포트폴리오 조회 — 에이전트 슬롯 진입 시 호출.

출력: JSON
{
  "agent_id": "opus-hunter-v1",
  "fetched_at": "...",
  "cash": 10000000,
  "equity_value": 3457900,
  "total_value": 13457900,
  "unrealized_pnl": -42100,
  "realized_pnl_cum": 0,
  "drawdown_pct": 0.0,
  "positions": [
    {
      "code": "005930", "name": "삼성전자", "qty": 10,
      "avg_price": 279418, "invested": 2794599,
      "current_price": 281000,
      "market_value": 2810000,
      "unrealized_pnl": 15401,
      "unrealized_pnl_pct": 0.55,
      "opened_at": "2026-05-12T09:15:30+09:00",
      "latest_thesis": "..."
    }
  ],
  "history_30d": [   # 자산곡선용 (최근 30일 슬롯 스냅샷)
    {"slot_ts": "...", "total_value": ...},
    ...
  ]
}

CLI:
  python scripts/ai_trader/get_portfolio.py
  python scripts/ai_trader/get_portfolio.py --agent-id opus-hunter-v1
  python scripts/ai_trader/get_portfolio.py --no-live  # 평가금액 갱신 안 함 (DB만)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from server.db.connections import get_stocks_conn  # noqa: E402
from collectors.kis_api import KISCollector  # noqa: E402

AGENT_ID_DEFAULT = "opus-hunter-v1"


def _safe_row_to_dict(row):
    if row is None:
        return None
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    return dict(row)


def _fetch_current_prices(codes: list[str]) -> dict[str, int]:
    """KIS get_price 로 다중 종목 현재가 조회 (순차 — 종목 수 적으니 OK)."""
    if not codes:
        return {}
    c = KISCollector()
    out: dict[str, int] = {}
    for code in codes:
        try:
            info = c.get_price(code)
            out[code] = int(info.get("current_price") or 0)
        except Exception:
            out[code] = 0
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent-id", default=AGENT_ID_DEFAULT)
    ap.add_argument("--no-live", action="store_true", help="현재가 갱신 안 함")
    ap.add_argument("--history-days", type=int, default=30)
    args = ap.parse_args()

    conn = get_stocks_conn()
    try:
        # 최신 state
        state_row = conn.execute(
            """
            SELECT cash, equity_value, total_value, unrealized_pnl,
                   realized_pnl_cum, drawdown_pct, slot_ts
              FROM ai_trader_state
             WHERE agent_id=?
             ORDER BY slot_ts DESC, id DESC LIMIT 1
            """,
            (args.agent_id,),
        ).fetchone()
        state = _safe_row_to_dict(state_row) or {}

        # 보유 종목
        pos_rows = conn.execute(
            """
            SELECT code, name, qty, avg_price, invested, opened_at, updated_at, latest_thesis
              FROM ai_trader_positions
             WHERE agent_id=? AND qty > 0
             ORDER BY invested DESC
            """,
            (args.agent_id,),
        ).fetchall()
        positions = [_safe_row_to_dict(r) for r in pos_rows]

        # 30일 자산 곡선
        hist_rows = conn.execute(
            """
            SELECT slot_ts, total_value, cash, equity_value
              FROM ai_trader_state
             WHERE agent_id=?
             ORDER BY slot_ts DESC LIMIT ?
            """,
            (args.agent_id, args.history_days * 6),  # 최대 6 슬롯/일
        ).fetchall()
        history = [_safe_row_to_dict(r) for r in hist_rows]
        history.reverse()

        # 실현손익 누적 (orders 합산 — state 컬럼이 비어있을 경우 대비)
        rl = conn.execute(
            "SELECT COALESCE(SUM(pnl_realized), 0) AS s FROM ai_trader_orders WHERE agent_id=?",
            (args.agent_id,),
        ).fetchone()
        realized_pnl_cum = float(rl["s"] if hasattr(rl, "keys") else rl[0])
    finally:
        conn.close()

    # 현재가 갱신
    current_prices: dict[str, int] = {}
    if not args.no_live and positions:
        current_prices = _fetch_current_prices([p["code"] for p in positions])

    cash = float(state.get("cash") or 0)
    equity_value = 0.0
    unrealized = 0.0
    pos_out = []
    for p in positions:
        code = p["code"]
        qty = int(p["qty"])
        avg = float(p["avg_price"])
        invested = float(p["invested"])
        cur = current_prices.get(code, 0)
        mv = cur * qty if cur > 0 else 0
        u_pnl = mv - invested if mv > 0 else 0
        u_pct = (u_pnl / invested * 100.0) if invested > 0 else 0
        equity_value += mv
        unrealized += u_pnl
        pos_out.append({
            "code": code,
            "name": p.get("name") or "",
            "qty": qty,
            "avg_price": avg,
            "invested": invested,
            "current_price": cur,
            "market_value": mv,
            "unrealized_pnl": u_pnl,
            "unrealized_pnl_pct": round(u_pct, 2),
            "opened_at": p.get("opened_at"),
            "updated_at": p.get("updated_at"),
            "latest_thesis": p.get("latest_thesis") or "",
        })

    total_value = cash + equity_value

    out = {
        "agent_id": args.agent_id,
        "fetched_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "cash": cash,
        "equity_value": equity_value,
        "total_value": total_value,
        "unrealized_pnl": unrealized,
        "realized_pnl_cum": realized_pnl_cum,
        "drawdown_pct": float(state.get("drawdown_pct") or 0),
        "last_state_slot_ts": state.get("slot_ts"),
        "positions": pos_out,
        "history_30d": history,
    }
    print(json.dumps(out, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
