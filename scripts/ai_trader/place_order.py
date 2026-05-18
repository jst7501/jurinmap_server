"""
ai_trader 가상 매매 실행 — 시뮬레이터로 체결가 결정 후 DB 4개 테이블 일관 갱신.

핵심 트랜잭션 흐름:
  1. simulate_fill 호출 → 체결가/수수료/세금/순체결액 결정
  2. 검증 (물리법칙만):
     - buy: cash >= net_amount (현금 부족 거부)
     - sell: position.qty >= qty (보유 부족 거부)
     - tradable_now (09:00-18:00 KST)
  3. ai_trader_orders insert
  4. ai_trader_positions upsert (buy: 평단 가중평균 / sell: 차감 또는 제거)
  5. ai_trader_state insert (이번 슬롯 스냅샷)

CLI:
  python scripts/ai_trader/place_order.py \
    --code 005930 --side buy --qty 10 \
    --slot-kind morning \
    --thesis "외인 3일 연속 매수 + HBM 호재 기대" \
    --journal-id 7   (선택, 저널 행 미리 있으면 연결)
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

from scripts.ai_trader.fill_simulator import simulate_fill, is_tradable_now  # noqa: E402

AGENT_ID_DEFAULT = "opus-hunter-v1"
SLOT_KINDS = {
    "pre_market", "open", "morning", "mid", "afternoon",
    "close", "post", "manual", "weekly_review",
}


def _safe_row(row):
    if row is None:
        return None
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    return dict(row)


def _fetch_name(collector: KISCollector, code: str) -> str:
    """KIS 가 종목명 직접 안 주는 API 들이 있어 stocks 테이블에서 조회 우선."""
    conn = get_stocks_conn()
    try:
        row = conn.execute(
            "SELECT name FROM stocks WHERE code=? LIMIT 1", (code,)
        ).fetchone()
        if row:
            n = row["name"] if hasattr(row, "keys") else row[0]
            if n:
                return str(n)
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return code  # fallback


def _get_latest_state(conn, agent_id: str) -> dict:
    row = conn.execute(
        """
        SELECT cash, equity_value, realized_pnl_cum
          FROM ai_trader_state
         WHERE agent_id=?
         ORDER BY slot_ts DESC, id DESC LIMIT 1
        """,
        (agent_id,),
    ).fetchone()
    return _safe_row(row) or {"cash": 0, "equity_value": 0, "realized_pnl_cum": 0}


def _get_position(conn, agent_id: str, code: str) -> dict | None:
    row = conn.execute(
        "SELECT id, qty, avg_price, invested, opened_at FROM ai_trader_positions "
        "WHERE agent_id=? AND code=?",
        (agent_id, code),
    ).fetchone()
    return _safe_row(row)


def _refresh_equity_value(conn, agent_id: str, collector: KISCollector) -> float:
    """모든 보유 종목 현재가 다시 fetch 해서 평가금액 합 계산."""
    rows = conn.execute(
        "SELECT code, qty FROM ai_trader_positions WHERE agent_id=? AND qty > 0",
        (agent_id,),
    ).fetchall()
    total = 0.0
    for r in rows:
        code = r["code"] if hasattr(r, "keys") else r[0]
        qty = int(r["qty"] if hasattr(r, "keys") else r[1])
        try:
            info = collector.get_price(code)
            cur = int(info.get("current_price") or 0)
        except Exception:
            cur = 0
        total += cur * qty
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent-id", default=AGENT_ID_DEFAULT)
    ap.add_argument("--code", required=True)
    ap.add_argument("--side", required=True, choices=["buy", "sell"])
    ap.add_argument("--qty", required=True, type=int)
    ap.add_argument("--slot-kind", required=True,
                    help=f"one of: {sorted(SLOT_KINDS)}")
    ap.add_argument("--slot-ts", default=None)
    ap.add_argument("--thesis", default="", help="왜 샀나/팔았나")
    ap.add_argument("--tags", default="", help="콤마구분 태그")
    ap.add_argument("--journal-id", type=int, default=None,
                    help="ai_trader_journal.id 연결")
    args = ap.parse_args()

    if args.slot_kind not in SLOT_KINDS:
        print(f"[place_order] ERROR: bad slot_kind {args.slot_kind}", file=sys.stderr)
        return 2

    now = datetime.now()
    slot_ts = args.slot_ts or now.strftime("%Y-%m-%dT%H:%M:%S+09:00")
    slot_date = slot_ts[:10]
    created_at = now.strftime("%Y-%m-%d %H:%M:%S")

    tradable, why = is_tradable_now(now)
    if not tradable:
        print(json.dumps({"ok": False, "reason": f"not_tradable: {why}"}, ensure_ascii=False))
        return 3

    collector = KISCollector()
    fill = simulate_fill(args.code, args.side, args.qty, collector=collector)
    if not fill.get("ok"):
        print(json.dumps({"ok": False, "reason": fill.get("reason")}, ensure_ascii=False))
        return 3
    fill["name"] = _fetch_name(collector, args.code)

    conn = get_stocks_conn()
    try:
        state = _get_latest_state(conn, args.agent_id)
        cash = float(state.get("cash") or 0)
        realized_cum = float(state.get("realized_pnl_cum") or 0)

        pos = _get_position(conn, args.agent_id, args.code)

        # ── 검증 (물리법칙만) ──────────────────────────────────
        if args.side == "buy":
            if cash < fill["net_amount"]:
                print(json.dumps({
                    "ok": False, "reason": "insufficient_cash",
                    "cash": cash, "needed": fill["net_amount"],
                }, ensure_ascii=False))
                return 4
        else:  # sell
            held = int(pos["qty"]) if pos else 0
            if held < args.qty:
                print(json.dumps({
                    "ok": False, "reason": "insufficient_position",
                    "held": held, "requested": args.qty,
                }, ensure_ascii=False))
                return 4

        # ── ai_trader_orders insert ────────────────────────────
        pnl_realized = 0.0
        if args.side == "sell" and pos:
            avg = float(pos["avg_price"])
            # 실현손익 = (체결가 - 평단)*qty - 수수료 - 세금
            pnl_realized = (fill["fill_price"] - avg) * args.qty - fill["commission"] - fill["tax"]

        cur = conn.execute(
            """
            INSERT INTO ai_trader_orders
                (agent_id, journal_id, slot_ts, slot_kind, code, name, side, qty,
                 requested_price, fill_price, slippage_pct, commission, tax,
                 net_amount, pnl_realized, thesis, tags, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                args.agent_id, args.journal_id, slot_ts, args.slot_kind,
                args.code, fill["name"], args.side, args.qty,
                fill["current_price"], fill["fill_price"], fill["slippage_pct"],
                fill["commission"], fill["tax"], fill["net_amount"],
                pnl_realized, args.thesis, args.tags, created_at,
            ),
        ).fetchone()
        order_id = int(cur["id"] if hasattr(cur, "keys") else cur[0])

        # ── ai_trader_positions upsert ─────────────────────────
        if args.side == "buy":
            if pos:
                new_qty = int(pos["qty"]) + args.qty
                new_invested = float(pos["invested"]) + fill["net_amount"]
                new_avg = new_invested / new_qty
                conn.execute(
                    """
                    UPDATE ai_trader_positions
                       SET qty=?, avg_price=?, invested=?, updated_at=?,
                           latest_thesis=COALESCE(NULLIF(?, ''), latest_thesis),
                           name=COALESCE(NULLIF(?, ''), name)
                     WHERE id=?
                    """,
                    (new_qty, new_avg, new_invested, created_at,
                     args.thesis, fill["name"], int(pos["id"])),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO ai_trader_positions
                        (agent_id, code, name, qty, avg_price, invested,
                         opened_at, updated_at, latest_thesis)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (args.agent_id, args.code, fill["name"], args.qty,
                     fill["fill_price"] * 1.0 + (fill["commission"] / max(args.qty, 1)),
                     fill["net_amount"], slot_ts, created_at, args.thesis),
                )
        else:  # sell
            assert pos  # 검증 통과 후
            new_qty = int(pos["qty"]) - args.qty
            if new_qty <= 0:
                conn.execute("DELETE FROM ai_trader_positions WHERE id=?", (int(pos["id"]),))
            else:
                # 평단·invested 비례 축소 — 평단 유지, invested 만 감소
                ratio = new_qty / int(pos["qty"])
                new_invested = float(pos["invested"]) * ratio
                conn.execute(
                    """
                    UPDATE ai_trader_positions
                       SET qty=?, invested=?, updated_at=?
                     WHERE id=?
                    """,
                    (new_qty, new_invested, created_at, int(pos["id"])),
                )

        # ── 현금 갱신 ─────────────────────────────────────────
        if args.side == "buy":
            new_cash = cash - fill["net_amount"]
        else:
            new_cash = cash + fill["net_amount"]
        new_realized_cum = realized_cum + pnl_realized

        # ── ai_trader_state 새 스냅샷 ─────────────────────────
        new_equity = _refresh_equity_value(conn, args.agent_id, collector)
        new_total = new_cash + new_equity

        # drawdown — 직전 최고가 대비
        max_row = conn.execute(
            "SELECT MAX(total_value) AS m FROM ai_trader_state WHERE agent_id=?",
            (args.agent_id,),
        ).fetchone()
        prev_max = float(max_row["m"] if hasattr(max_row, "keys") else max_row[0]) if max_row else new_total
        if prev_max <= 0:
            prev_max = new_total
        dd = ((new_total - prev_max) / prev_max * 100.0) if prev_max > 0 else 0.0

        conn.execute(
            """
            INSERT INTO ai_trader_state
                (agent_id, slot_ts, slot_date, slot_kind, cash, equity_value,
                 total_value, unrealized_pnl, realized_pnl_cum, drawdown_pct,
                 kospi_index, kosdaq_index, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            ON CONFLICT (agent_id, slot_ts) DO UPDATE SET
                cash=excluded.cash,
                equity_value=excluded.equity_value,
                total_value=excluded.total_value,
                unrealized_pnl=excluded.unrealized_pnl,
                realized_pnl_cum=excluded.realized_pnl_cum,
                drawdown_pct=excluded.drawdown_pct
            """,
            (args.agent_id, slot_ts, slot_date, args.slot_kind, new_cash,
             new_equity, new_total, 0, new_realized_cum, dd, created_at),
        )

        conn.commit()
    finally:
        conn.close()

    print(json.dumps({
        "ok": True,
        "order_id": order_id,
        "code": args.code, "name": fill["name"], "side": args.side, "qty": args.qty,
        "fill_price": fill["fill_price"],
        "net_amount": fill["net_amount"],
        "commission": fill["commission"],
        "tax": fill["tax"],
        "pnl_realized": pnl_realized,
        "new_cash": new_cash,
        "new_total_value": new_total,
        "drawdown_pct": dd,
    }, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
