"""
ai_trader_journal upsert — 슬롯별 의사결정 로그 기록.

각 슬롯에서 에이전트가 깨어나 thinking 끝낸 직후 호출.
mood / weather / observations / thinking / decision / web_searches 등을 JSON 으로 받아 저장.

(market_briefings 의 ai_briefing_upsert.py 와 유사 패턴.)

사용법:
  # 인라인
  python scripts/ai_trader/journal_upsert.py \
    --slot-kind morning \
    --mood "관망 모드 — 외인 흐릿" \
    --weather "코스피 횡보 + 외인 +0.3조 약매수" \
    --decision-kind skip \
    --thinking-file /tmp/think.md

  # stdin (긴 thinking)
  cat thinking.md | python scripts/ai_trader/journal_upsert.py \
    --slot-kind morning --decision-kind skip --thinking-stdin
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

AGENT_ID_DEFAULT = "opus-hunter-v1"
DECISION_KINDS = {"skip", "observe", "buy", "sell", "mixed", "rebalance"}
SLOT_KINDS = {
    "pre_market", "open", "morning", "mid", "afternoon",
    "close", "post", "manual", "weekly_review",
}


def _read_file_or_arg(path: str | None, inline: str | None, stdin_flag: bool) -> str:
    if stdin_flag:
        return sys.stdin.read().strip()
    if path:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return (inline or "").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent-id", default=AGENT_ID_DEFAULT)
    ap.add_argument("--slot-kind", required=True,
                    help=f"one of: {sorted(SLOT_KINDS)}")
    ap.add_argument("--slot-ts", default=None, help="ISO ts, default now KST")
    ap.add_argument("--model", default="claude-opus-4")

    ap.add_argument("--mood", default="", help="한 줄 자기 상태")
    ap.add_argument("--weather", default="", help="시장 한 줄 진단")

    ap.add_argument("--observations", default=None, help="본 것 (markdown)")
    ap.add_argument("--observations-file", default=None)

    ap.add_argument("--thinking", default=None, help="생각의 흐름 (markdown)")
    ap.add_argument("--thinking-file", default=None)
    ap.add_argument("--thinking-stdin", action="store_true")

    ap.add_argument("--decision-kind", required=True,
                    help=f"one of: {sorted(DECISION_KINDS)}")
    ap.add_argument("--regret", default=None, help="직전 슬롯 결정 회고")
    ap.add_argument("--regret-file", default=None)

    ap.add_argument("--highlight-quote", default="",
                    help="어록 노출용 한 줄 (선택)")

    ap.add_argument("--web-searches-json", default=None,
                    help='[{"q":"...","urls":[{"url":"...","title":"...","summary":"..."}]}]')
    ap.add_argument("--data-sources-json", default=None,
                    help='[{"tool":"get_market_context","summary":"..."}]')
    ap.add_argument("--key-data-json", default=None,
                    help='구조화 데이터 JSON 문자열 (indices/flow_kr/holdings_impact/watchlist/decision_summary)')
    ap.add_argument("--key-data-file", default=None,
                    help='key_data JSON 파일 경로')

    ap.add_argument("--prompt-tokens", type=int, default=None)
    ap.add_argument("--completion-tokens", type=int, default=None)
    ap.add_argument("--cost-usd", type=float, default=None)

    args = ap.parse_args()

    if args.slot_kind not in SLOT_KINDS:
        print(f"[journal_upsert] ERROR: slot_kind not in {sorted(SLOT_KINDS)}", file=sys.stderr)
        return 2
    if args.decision_kind not in DECISION_KINDS:
        print(f"[journal_upsert] ERROR: decision_kind not in {sorted(DECISION_KINDS)}", file=sys.stderr)
        return 2

    now = datetime.now()
    slot_ts = args.slot_ts or now.strftime("%Y-%m-%dT%H:%M:%S+09:00")
    slot_date = slot_ts[:10]
    created_at = now.strftime("%Y-%m-%d %H:%M:%S")

    observations_md = _read_file_or_arg(args.observations_file, args.observations, False)
    thinking_md = _read_file_or_arg(args.thinking_file, args.thinking, args.thinking_stdin)
    regret_md = _read_file_or_arg(args.regret_file, args.regret, False)

    # JSON 검증
    web_searches_json = args.web_searches_json
    if web_searches_json:
        try:
            json.loads(web_searches_json)
        except Exception as e:
            print(f"[journal_upsert] ERROR: web-searches-json parse fail {e}", file=sys.stderr)
            return 2
    data_sources_json = args.data_sources_json
    if data_sources_json:
        try:
            json.loads(data_sources_json)
        except Exception as e:
            print(f"[journal_upsert] ERROR: data-sources-json parse fail {e}", file=sys.stderr)
            return 2

    # key_data — 파일 또는 인라인
    key_data_json = args.key_data_json
    if args.key_data_file:
        with open(args.key_data_file, "r", encoding="utf-8") as f:
            key_data_json = f.read().strip()
    if key_data_json:
        try:
            json.loads(key_data_json)
        except Exception as e:
            print(f"[journal_upsert] ERROR: key-data-json parse fail {e}", file=sys.stderr)
            return 2

    conn = get_stocks_conn()
    try:
        # upsert by (agent_id, slot_ts)
        row = conn.execute(
            "SELECT id FROM ai_trader_journal WHERE agent_id=? AND slot_ts=?",
            (args.agent_id, slot_ts),
        ).fetchone()
        existing_id = None
        if row:
            existing_id = int(row["id"] if hasattr(row, "keys") else row[0])

        if existing_id:
            conn.execute(
                """
                UPDATE ai_trader_journal SET
                    slot_date=?, slot_kind=?, model=?, mood=?, weather=?,
                    observations_md=?, thinking_md=?, web_searches_json=?,
                    data_sources_json=?, key_data_json=?, decision_kind=?, regret_md=?,
                    highlight_quote=?, prompt_tokens=?, completion_tokens=?,
                    cost_usd=?, created_at=?
                WHERE id=?
                """,
                (
                    slot_date, args.slot_kind, args.model, args.mood, args.weather,
                    observations_md, thinking_md, web_searches_json,
                    data_sources_json, key_data_json, args.decision_kind, regret_md,
                    args.highlight_quote, args.prompt_tokens, args.completion_tokens,
                    args.cost_usd, created_at, existing_id,
                ),
            )
            journal_id = existing_id
        else:
            cur = conn.execute(
                """
                INSERT INTO ai_trader_journal
                    (agent_id, slot_ts, slot_date, slot_kind, model, mood, weather,
                     observations_md, thinking_md, web_searches_json, data_sources_json,
                     key_data_json, decision_kind, regret_md, highlight_quote,
                     prompt_tokens, completion_tokens, cost_usd, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    args.agent_id, slot_ts, slot_date, args.slot_kind, args.model,
                    args.mood, args.weather, observations_md, thinking_md,
                    web_searches_json, data_sources_json, key_data_json,
                    args.decision_kind, regret_md, args.highlight_quote,
                    args.prompt_tokens, args.completion_tokens, args.cost_usd, created_at,
                ),
            ).fetchone()
            journal_id = int(cur["id"] if hasattr(cur, "keys") else cur[0])

        conn.commit()
    finally:
        conn.close()

    print(json.dumps({
        "ok": True,
        "journal_id": journal_id,
        "agent_id": args.agent_id,
        "slot_ts": slot_ts,
        "slot_kind": args.slot_kind,
        "decision_kind": args.decision_kind,
        "thinking_len": len(thinking_md),
        "highlight": args.highlight_quote,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
