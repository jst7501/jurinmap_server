"""
ai_trader DB 초기화 — 4개 테이블 + 초기 자금 부트스트랩.

테이블:
  ai_trader_state      — 슬롯별 자산 스냅샷 (시계열)
  ai_trader_positions  — 현재 보유 (1행/종목)
  ai_trader_orders     — 모든 주문 히스토리
  ai_trader_journal    — 의사결정 로그 (mood/observations/thinking/decision)

사용:
  python scripts/ai_trader/init_db.py           # 테이블 생성 + 초기 자금 부트스트랩
  python scripts/ai_trader/init_db.py --reset   # 모든 ai_trader_* 데이터 삭제 후 재부트스트랩 (위험)
"""
from __future__ import annotations

import argparse
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

INITIAL_CASH = 10_000_000  # ₩1천만
AGENT_ID = "opus-hunter-v1"  # 페르소나 식별자 (나중에 페르소나 분기 시 다른 ID 추가)


def _ensure_tables(conn) -> None:
    """4개 테이블 + 인덱스 생성. 멱등."""

    # 1) ai_trader_state — 슬롯별 자산 스냅샷
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_trader_state (
            id SERIAL PRIMARY KEY,
            agent_id TEXT NOT NULL,
            slot_ts TEXT NOT NULL,           -- '2026-05-12T09:15:00+09:00'
            slot_date TEXT NOT NULL,         -- '2026-05-12'
            slot_kind TEXT NOT NULL,         -- 'pre_market'|'open'|'morning'|'mid'|'afternoon'|'close'|'post'|'manual'
            cash NUMERIC NOT NULL,           -- 현금 (원)
            equity_value NUMERIC NOT NULL,   -- 평가금액 합 (원)
            total_value NUMERIC NOT NULL,    -- cash + equity_value
            unrealized_pnl NUMERIC NOT NULL DEFAULT 0,
            realized_pnl_cum NUMERIC NOT NULL DEFAULT 0,
            drawdown_pct NUMERIC NOT NULL DEFAULT 0,   -- 진입 후 최고가 대비 음수 퍼센트
            kospi_index NUMERIC,             -- 벤치마크 동시점
            kosdaq_index NUMERIC,
            created_at TEXT NOT NULL,
            UNIQUE (agent_id, slot_ts)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_trader_state_agent_date "
        "ON ai_trader_state(agent_id, slot_date DESC)"
    )

    # 2) ai_trader_positions — 현재 보유 (1행/종목)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_trader_positions (
            id SERIAL PRIMARY KEY,
            agent_id TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            qty INTEGER NOT NULL,             -- 주식 수
            avg_price NUMERIC NOT NULL,       -- 평단 (원, 매수 비용 가중)
            invested NUMERIC NOT NULL,        -- 총 투자금액 (수수료 포함)
            opened_at TEXT NOT NULL,          -- 최초 진입 시각
            updated_at TEXT NOT NULL,
            latest_thesis TEXT,               -- 가장 최근 매수 thesis (왜 들고 있나)
            UNIQUE (agent_id, code)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_trader_positions_agent "
        "ON ai_trader_positions(agent_id)"
    )

    # 3) ai_trader_orders — 모든 주문 히스토리
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_trader_orders (
            id SERIAL PRIMARY KEY,
            agent_id TEXT NOT NULL,
            journal_id INTEGER,                -- ai_trader_journal.id 와 연결
            slot_ts TEXT NOT NULL,
            slot_kind TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            side TEXT NOT NULL,                -- 'buy'|'sell'
            qty INTEGER NOT NULL,
            requested_price NUMERIC,           -- 결정 시각 마지막 시세 (참고)
            fill_price NUMERIC NOT NULL,       -- 슬리피지·세금 미반영 체결가
            slippage_pct NUMERIC NOT NULL DEFAULT 0,
            commission NUMERIC NOT NULL DEFAULT 0,
            tax NUMERIC NOT NULL DEFAULT 0,    -- 매도세 (매도 시만)
            net_amount NUMERIC NOT NULL,       -- buy: -지출, sell: +수입 (수수료·세금 반영)
            pnl_realized NUMERIC NOT NULL DEFAULT 0,    -- 매도 시 실현손익
            thesis TEXT,                       -- "왜 샀나/팔았나" 한 단락
            tags TEXT,                         -- 'momentum,foreigner,新高가' 같은 태그
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_trader_orders_agent_ts "
        "ON ai_trader_orders(agent_id, slot_ts DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_trader_orders_journal "
        "ON ai_trader_orders(journal_id)"
    )

    # 4) ai_trader_journal — 의사결정 로그 (영혼)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_trader_journal (
            id SERIAL PRIMARY KEY,
            agent_id TEXT NOT NULL,
            slot_ts TEXT NOT NULL,
            slot_date TEXT NOT NULL,
            slot_kind TEXT NOT NULL,
            model TEXT,                        -- 'claude-opus-4'
            mood TEXT,                         -- 한 줄 자기 상태 ("관망 모드, 외인 행보 흐릿")
            weather TEXT,                      -- 시장 한 줄 진단 ("코스피 횡보 + 외인 +0.3조 약매수")
            observations_md TEXT,              -- 본 것 (지수·테마·종목·뉴스)
            thinking_md TEXT,                  -- 생각의 흐름 (가설·반증·결정)
            web_searches_json TEXT,            -- [{q, urls:[{url,title,summary}]}]
            data_sources_json TEXT,            -- 내부 API 호출 기록 [{tool, params, summary}]
            key_data_json TEXT,                -- 구조화된 핵심 데이터 (indices/flow/holdings/watchlist/decision)
            decision_kind TEXT,                -- 'skip'|'observe'|'buy'|'sell'|'mixed'|'rebalance'
            orders_executed_json TEXT,         -- 실제 체결된 주문 요약
            regret_md TEXT,                    -- 직전 슬롯 결정 회고 (선택)
            highlight_quote TEXT,              -- 어록 — 본인이 강조한 한 줄 (어록 페이지 노출용)
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            cost_usd NUMERIC,
            created_at TEXT NOT NULL,
            UNIQUE (agent_id, slot_ts)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_trader_journal_agent_date "
        "ON ai_trader_journal(agent_id, slot_date DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_trader_journal_highlight "
        "ON ai_trader_journal(agent_id, highlight_quote) WHERE highlight_quote IS NOT NULL"
    )

    try:
        conn.commit()
    except Exception:
        pass


def _bootstrap_initial_state(conn, agent_id: str, initial_cash: int) -> None:
    """현재 state 행이 하나도 없으면 초기 자금 행 1개 박음."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM ai_trader_state WHERE agent_id=?",
        (agent_id,),
    ).fetchone()
    n = int(row["n"] if hasattr(row, "keys") else row[0])
    if n > 0:
        print(f"[init_db] state rows already exist (n={n}). skip bootstrap.")
        return

    now = datetime.now()
    slot_ts = now.strftime("%Y-%m-%dT%H:%M:%S+09:00")
    slot_date = now.strftime("%Y-%m-%d")
    created_at = now.strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        INSERT INTO ai_trader_state
            (agent_id, slot_ts, slot_date, slot_kind, cash, equity_value,
             total_value, unrealized_pnl, realized_pnl_cum, drawdown_pct,
             kospi_index, kosdaq_index, created_at)
        VALUES (?, ?, ?, 'bootstrap', ?, 0, ?, 0, 0, 0, NULL, NULL, ?)
        """,
        (agent_id, slot_ts, slot_date, initial_cash, initial_cash, created_at),
    )
    try:
        conn.commit()
    except Exception:
        pass
    print(
        f"[init_db] bootstrapped agent={agent_id} cash=KRW {initial_cash:,} "
        f"at slot_ts={slot_ts}"
    )


def _reset(conn, agent_id: str) -> None:
    for tbl in ("ai_trader_orders", "ai_trader_positions", "ai_trader_state", "ai_trader_journal"):
        conn.execute(f"DELETE FROM {tbl} WHERE agent_id=?", (agent_id,))
    try:
        conn.commit()
    except Exception:
        pass
    print(f"[init_db] RESET — all rows deleted for agent={agent_id}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent-id", default=AGENT_ID)
    ap.add_argument("--initial-cash", type=int, default=INITIAL_CASH)
    ap.add_argument("--reset", action="store_true", help="기존 데이터 삭제 후 재부트스트랩 (위험)")
    args = ap.parse_args()

    conn = get_stocks_conn()
    try:
        _ensure_tables(conn)
        print("[init_db] tables ensured (ai_trader_state/positions/orders/journal)")
        if args.reset:
            _reset(conn, args.agent_id)
        _bootstrap_initial_state(conn, args.agent_id, args.initial_cash)
    finally:
        conn.close()

    print("[init_db] done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
