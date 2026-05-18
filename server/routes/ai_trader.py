"""
ai_trader 라우터 — 가상 매매 에이전트 (Opus-Hunter v1) 의 상태·매매·저널을 프론트에 노출.

엔드포인트 (모두 read-only — 쓰기는 scripts/ai_trader/*.py 만):
  GET /api/ai-trader/state         최신 상태 + 보유 (한 번에)
  GET /api/ai-trader/curve         자산 곡선 (state 시계열 + KOSPI 정규화)
  GET /api/ai-trader/positions     현재 보유 종목 상세
  GET /api/ai-trader/orders        매매 히스토리 (페이징)
  GET /api/ai-trader/journal       저널 (slot 단위)
  GET /api/ai-trader/journal/{id}  저널 단건 (thinking_md 전문)
  GET /api/ai-trader/highlights    어록 (highlight_quote 만)
  GET /api/ai-trader/stats         통계 (승률·평균보유일·MDD)

agent_id 쿼리로 페르소나 선택 (디폴트 opus-hunter-v1)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException

from ..db.connections import get_stocks_conn

logger = logging.getLogger("server.routes.ai_trader")
router = APIRouter(prefix="/api/ai-trader", tags=["ai_trader"])

DEFAULT_AGENT = "opus-hunter-v1"


def _row_to_dict(row):
    if row is None:
        return None
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    return dict(row)


def _parse_json(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


@router.get("/state")
def get_state(agent_id: str = DEFAULT_AGENT):
    """최신 자산 스냅샷 + 보유 종목 + 누적 실현손익."""
    conn = get_stocks_conn()
    try:
        # 최신 state 1행
        state_row = conn.execute(
            """
            SELECT cash, equity_value, total_value, unrealized_pnl,
                   realized_pnl_cum, drawdown_pct, slot_ts, slot_kind, kospi_index
              FROM ai_trader_state
             WHERE agent_id=?
             ORDER BY slot_ts DESC, id DESC LIMIT 1
            """,
            (agent_id,),
        ).fetchone()
        state = _row_to_dict(state_row) or {}

        # 보유 + price_today 캐시에서 현재가 가져옴 (KIS 실시간 호출 X)
        pos_rows = conn.execute(
            """
            SELECT p.code, p.name, p.qty, p.avg_price, p.invested,
                   p.opened_at, p.updated_at, p.latest_thesis,
                   pt.current_price, pt.change_pct
              FROM ai_trader_positions p
              LEFT JOIN price_today pt ON pt.code = p.code
             WHERE p.agent_id=? AND p.qty > 0
             ORDER BY p.invested DESC
            """,
            (agent_id,),
        ).fetchall()
        positions = []
        for r in pos_rows:
            d = _row_to_dict(r)
            cur_price = float(d.get("current_price") or 0)
            qty = int(d.get("qty") or 0)
            avg = float(d.get("avg_price") or 0)
            invested = float(d.get("invested") or 0)
            mv = cur_price * qty if cur_price > 0 else 0
            u_pnl = mv - invested if mv > 0 else 0
            u_pnl_pct = (u_pnl / invested * 100.0) if invested > 0 else 0
            d["market_value"] = mv
            d["unrealized_pnl"] = u_pnl
            d["unrealized_pnl_pct"] = round(u_pnl_pct, 2)
            positions.append(d)

        # 누적 통계
        agg = conn.execute(
            """
            SELECT COUNT(*) AS n_orders,
                   COUNT(CASE WHEN side='buy' THEN 1 END) AS n_buys,
                   COUNT(CASE WHEN side='sell' THEN 1 END) AS n_sells,
                   COALESCE(SUM(pnl_realized), 0) AS realized
              FROM ai_trader_orders
             WHERE agent_id=?
            """,
            (agent_id,),
        ).fetchone()
        agg_d = _row_to_dict(agg) or {}
    finally:
        conn.close()

    initial_cash = 10_000_000
    # positions 의 unrealized_pnl 합산 (price_today 기준 최신)
    unrealized_sum = sum(float(p.get("unrealized_pnl") or 0) for p in positions)
    equity_live = sum(float(p.get("market_value") or 0) for p in positions)
    cash_v = float(state.get("cash") or 0)
    total = cash_v + equity_live if equity_live > 0 else float(state.get("total_value") or initial_cash)
    return {
        "agent_id": agent_id,
        "initial_cash": initial_cash,
        "current": {
            "cash": cash_v,
            "equity_value": equity_live if equity_live > 0 else float(state.get("equity_value") or 0),
            "total_value": total,
            "unrealized_pnl": unrealized_sum,
            "realized_pnl_cum": float(state.get("realized_pnl_cum") or 0),
            "drawdown_pct": float(state.get("drawdown_pct") or 0),
            "return_pct": round((total - initial_cash) / initial_cash * 100, 4),
            "as_of_slot": state.get("slot_ts"),
            "as_of_slot_kind": state.get("slot_kind"),
        },
        "positions": positions,
        "stats": {
            "n_orders": int(agg_d.get("n_orders") or 0),
            "n_buys": int(agg_d.get("n_buys") or 0),
            "n_sells": int(agg_d.get("n_sells") or 0),
            "realized_pnl_cum": float(agg_d.get("realized") or 0),
        },
    }


@router.get("/curve")
def get_curve(agent_id: str = DEFAULT_AGENT, days: int = 30):
    """자산 곡선 — state 시계열 (정규화 가능). KOSPI 동시점 없을 수도 있음."""
    days = max(1, min(int(days), 365))
    conn = get_stocks_conn()
    try:
        rows = conn.execute(
            """
            SELECT slot_ts, slot_date, slot_kind, cash, equity_value, total_value,
                   realized_pnl_cum, drawdown_pct, kospi_index
              FROM ai_trader_state
             WHERE agent_id=?
             ORDER BY slot_ts DESC LIMIT ?
            """,
            (agent_id, days * 7),  # 최대 7 슬롯/일
        ).fetchall()
    finally:
        conn.close()

    curve = [_row_to_dict(r) for r in rows]
    curve.reverse()
    return {"agent_id": agent_id, "days": days, "points": curve}


@router.get("/positions")
def get_positions(agent_id: str = DEFAULT_AGENT, include_history: bool = False):
    """현재 보유 + 종목별 진입~현재 매매 히스토리 (옵션)."""
    conn = get_stocks_conn()
    try:
        pos_rows = conn.execute(
            """
            SELECT code, name, qty, avg_price, invested,
                   opened_at, updated_at, latest_thesis
              FROM ai_trader_positions
             WHERE agent_id=? AND qty > 0
             ORDER BY invested DESC
            """,
            (agent_id,),
        ).fetchall()
        positions = [_row_to_dict(r) for r in pos_rows]

        if include_history:
            for p in positions:
                history_rows = conn.execute(
                    """
                    SELECT id, slot_ts, slot_kind, side, qty, fill_price,
                           net_amount, pnl_realized, thesis, tags, created_at
                      FROM ai_trader_orders
                     WHERE agent_id=? AND code=?
                     ORDER BY slot_ts ASC, id ASC
                    """,
                    (agent_id, p["code"]),
                ).fetchall()
                p["history"] = [_row_to_dict(r) for r in history_rows]
    finally:
        conn.close()

    return {"agent_id": agent_id, "positions": positions}


@router.get("/orders")
def get_orders(
    agent_id: str = DEFAULT_AGENT,
    limit: int = 50,
    offset: int = 0,
    code: Optional[str] = None,
):
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    conn = get_stocks_conn()
    try:
        sql = (
            "SELECT id, slot_ts, slot_kind, code, name, side, qty, "
            "requested_price, fill_price, slippage_pct, commission, tax, "
            "net_amount, pnl_realized, thesis, tags, journal_id, created_at "
            "FROM ai_trader_orders WHERE agent_id=? "
        )
        params: tuple = (agent_id,)
        if code:
            sql += "AND code=? "
            params = (agent_id, code.strip())
        sql += "ORDER BY slot_ts DESC, id DESC LIMIT ? OFFSET ?"
        params = params + (limit, offset)
        rows = conn.execute(sql, params).fetchall()
        orders = [_row_to_dict(r) for r in rows]
    finally:
        conn.close()
    return {"agent_id": agent_id, "orders": orders, "limit": limit, "offset": offset}


@router.get("/journal")
def get_journal(
    agent_id: str = DEFAULT_AGENT,
    limit: int = 30,
    offset: int = 0,
    date: Optional[str] = None,
):
    """저널 리스트 — 풀 본문 다 포함. 클릭 시 별도 fetch 불필요."""
    limit = max(1, min(int(limit or 30), 200))
    offset = max(0, int(offset or 0))
    conn = get_stocks_conn()
    try:
        sql = (
            "SELECT id, slot_ts, slot_date, slot_kind, model, mood, weather, "
            "decision_kind, highlight_quote, "
            "observations_md, thinking_md, regret_md, "
            "LENGTH(COALESCE(thinking_md,'')) AS thinking_len, "
            "data_sources_json, web_searches_json, key_data_json, "
            "prompt_tokens, completion_tokens, cost_usd, created_at "
            "FROM ai_trader_journal WHERE agent_id=? "
        )
        params: tuple = (agent_id,)
        if date:
            sql += "AND slot_date=? "
            params = params + (date,)
        sql += "ORDER BY slot_ts DESC LIMIT ? OFFSET ?"
        params = params + (limit, offset)
        rows = conn.execute(sql, params).fetchall()
        entries = []
        for r in rows:
            d = _row_to_dict(r)
            d["data_sources"] = _parse_json(d.pop("data_sources_json", None))
            d["web_searches"] = _parse_json(d.pop("web_searches_json", None))
            d["key_data"] = _parse_json(d.pop("key_data_json", None))
            entries.append(d)
    finally:
        conn.close()
    return {"agent_id": agent_id, "entries": entries, "limit": limit, "offset": offset}


@router.get("/journal/{journal_id}")
def get_journal_one(journal_id: int, agent_id: str = DEFAULT_AGENT):
    """저널 단건 전문 — 연결된 orders 함께."""
    conn = get_stocks_conn()
    try:
        row = conn.execute(
            "SELECT * FROM ai_trader_journal WHERE id=? AND agent_id=?",
            (journal_id, agent_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "journal not found")
        entry = _row_to_dict(row)
        entry["data_sources"] = _parse_json(entry.pop("data_sources_json", None))
        entry["web_searches"] = _parse_json(entry.pop("web_searches_json", None))
        entry["key_data"] = _parse_json(entry.pop("key_data_json", None))

        order_rows = conn.execute(
            "SELECT id, slot_ts, code, name, side, qty, fill_price, "
            "net_amount, pnl_realized, thesis, tags "
            "FROM ai_trader_orders "
            "WHERE journal_id=? AND agent_id=? "
            "ORDER BY id ASC",
            (journal_id, agent_id),
        ).fetchall()
        entry["orders"] = [_row_to_dict(r) for r in order_rows]
    finally:
        conn.close()
    return entry


@router.get("/highlights")
def get_highlights(agent_id: str = DEFAULT_AGENT, limit: int = 20):
    """어록 — highlight_quote 가 있는 저널 행만 최신순."""
    limit = max(1, min(int(limit or 20), 100))
    conn = get_stocks_conn()
    try:
        rows = conn.execute(
            "SELECT id, slot_ts, slot_kind, mood, highlight_quote, created_at "
            "FROM ai_trader_journal "
            "WHERE agent_id=? AND highlight_quote IS NOT NULL AND highlight_quote <> '' "
            "ORDER BY slot_ts DESC LIMIT ?",
            (agent_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return {"agent_id": agent_id, "highlights": [_row_to_dict(r) for r in rows]}


@router.get("/stats")
def get_stats(agent_id: str = DEFAULT_AGENT):
    """승률·평균보유일·최대낙폭 등 누적 통계."""
    conn = get_stocks_conn()
    try:
        # 실현손익 분포
        wl = conn.execute(
            """
            SELECT
              COUNT(CASE WHEN side='sell' AND pnl_realized > 0 THEN 1 END) AS wins,
              COUNT(CASE WHEN side='sell' AND pnl_realized < 0 THEN 1 END) AS losses,
              COUNT(CASE WHEN side='sell' AND pnl_realized = 0 THEN 1 END) AS flats,
              COALESCE(AVG(CASE WHEN side='sell' AND pnl_realized > 0 THEN pnl_realized END), 0) AS avg_win,
              COALESCE(AVG(CASE WHEN side='sell' AND pnl_realized < 0 THEN pnl_realized END), 0) AS avg_loss,
              COALESCE(SUM(pnl_realized), 0) AS realized_cum,
              COUNT(*) AS total_orders
            FROM ai_trader_orders WHERE agent_id=?
            """,
            (agent_id,),
        ).fetchone()
        wl_d = _row_to_dict(wl) or {}

        # MDD — state 시계열에서 직접
        mdd_row = conn.execute(
            "SELECT MIN(drawdown_pct) AS mdd FROM ai_trader_state WHERE agent_id=?",
            (agent_id,),
        ).fetchone()
        mdd = float((mdd_row["mdd"] if hasattr(mdd_row, "keys") else mdd_row[0]) or 0)

        # 슬롯 활동 (decision_kind 분포)
        decisions_rows = conn.execute(
            """
            SELECT decision_kind, COUNT(*) AS n
              FROM ai_trader_journal
             WHERE agent_id=?
             GROUP BY decision_kind
            """,
            (agent_id,),
        ).fetchall()
        decisions = {r["decision_kind"] if hasattr(r, "keys") else r[0]:
                     int(r["n"] if hasattr(r, "keys") else r[1])
                     for r in decisions_rows}
    finally:
        conn.close()

    wins = int(wl_d.get("wins") or 0)
    losses = int(wl_d.get("losses") or 0)
    closed = wins + losses
    win_rate = round(wins / closed * 100, 2) if closed > 0 else 0
    avg_win = float(wl_d.get("avg_win") or 0)
    avg_loss = float(wl_d.get("avg_loss") or 0)
    expectancy = (win_rate / 100 * avg_win) + ((100 - win_rate) / 100 * avg_loss)

    return {
        "agent_id": agent_id,
        "win_rate_pct": win_rate,
        "wins": wins,
        "losses": losses,
        "flats": int(wl_d.get("flats") or 0),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy_per_trade": round(expectancy, 0),
        "realized_pnl_cum": float(wl_d.get("realized_cum") or 0),
        "max_drawdown_pct": mdd,
        "total_orders": int(wl_d.get("total_orders") or 0),
        "decisions_dist": decisions,
    }
