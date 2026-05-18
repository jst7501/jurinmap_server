"""
AI 시황 브리핑 upsert — market_briefings 테이블에 저장.

Claude Code 루틴이 매 30분마다 호출. 직접 데이터 조회 + summary 생성한 뒤 이 스크립트로 기록.
AI API 호출하지 않음 (Claude Code 본인이 이미 thinking으로 생성).

사용법:
    python scripts/ai_briefing_upsert.py \
      --market KOSPI \
      --slot intra \
      --summary "코스피 +0.82% 반등 ... (전문)" \
      --context-json-file /tmp/brief_ctx.json

    # summary 길 때는 stdin에서 읽기도 가능
    echo "코스피 ..." | python scripts/ai_briefing_upsert.py --market KOSPI --slot intra --summary-stdin

slot: pre / intra / post 중 하나.
briefing_date / slot_time 은 생략 시 실행 시각에서 자동 설정.
같은 (market, briefing_date, slot) 조합이면 덮어씀.
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


from server.db.connections import get_stocks_conn  # noqa: E402

MODEL_TAG = "claude-code-routine"


def _now_parts():
    now = datetime.now()
    return (
        now.strftime("%Y-%m-%d"),
        now.strftime("%H:%M"),
        now.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _ensure_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_briefings (
            id SERIAL PRIMARY KEY,
            market TEXT NOT NULL,
            slot TEXT NOT NULL,
            briefing_date TEXT NOT NULL,
            slot_time TEXT,
            summary TEXT,
            context_json TEXT,
            model TEXT,
            created_at TEXT,
            UNIQUE (market, briefing_date, slot)
        )
        """
    )
    try:
        conn.commit()
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", required=True, choices=["KOSPI", "KOSDAQ", "NASDAQ", "SP500"])
    ap.add_argument(
        "--slot",
        required=True,
        choices=["pre", "morning", "afternoon", "post", "evening", "intra"],
        help="intra 는 legacy (2026-04-24 이후 morning/afternoon 사용 권장)",
    )
    ap.add_argument("--summary", default=None, help="요약 문장 (생략 시 --summary-stdin 필요)")
    ap.add_argument("--summary-stdin", action="store_true", help="summary 를 stdin 에서 읽음")
    ap.add_argument("--context-json-file", default=None, help="context JSON 파일 경로")
    ap.add_argument("--context-json", default=None, help="context JSON 문자열")
    ap.add_argument("--briefing-date", default=None, help="YYYY-MM-DD, 기본 오늘")
    ap.add_argument("--slot-time", default=None, help="HH:MM, 기본 지금")
    ap.add_argument("--force", action="store_true",
                    help="context_json 날짜 mismatch 정합성 검증 무시 (확신 시에만)")
    args = ap.parse_args()

    d, t, created = _now_parts()
    briefing_date = args.briefing_date or d
    slot_time = args.slot_time or t

    summary = args.summary
    if args.summary_stdin or summary is None:
        summary = (summary or "") + sys.stdin.read()
    summary = (summary or "").strip()
    if not summary:
        print("[ai_briefing_upsert] ERROR: summary 비어있음", file=sys.stderr)
        return 2

    ctx_raw = None
    if args.context_json_file:
        with open(args.context_json_file, "r", encoding="utf-8") as f:
            ctx_raw = f.read().strip()
    elif args.context_json:
        ctx_raw = args.context_json.strip()

    # ── 2026-05-11 추가 가드 — 빈 context_json 거부 ──────────────
    # context_json 인자 자체가 없거나 비어 있으면 화면에 헤드라인·bullets·indices·flow 가 모두 비어
    # 보이는 "시황이 있다 없다" 증상의 원인. summary 한 줄만 들어가는 row 차단.
    if not ctx_raw or ctx_raw in ("{}", "[]", "null"):
        if not getattr(args, "force", False):
            print(
                "[ai_briefing_upsert] REJECT: context_json 비어있음 — summary 만 단독 저장 금지. "
                "최소 summary_structured.headline + bullets 4개 작성 후 다시. (--force 로 강행)",
                file=sys.stderr,
            )
            return 4

    if ctx_raw:
        try:
            parsed_ctx = json.loads(ctx_raw)
        except Exception as e:
            print(f"[ai_briefing_upsert] ERROR: context-json 파싱 실패 {e}", file=sys.stderr)
            return 2

        # ── 2026-05-11 정합성 가드 — mismatch row 생성 차단 ──────────
        # context_json 의 날짜·테마 데이터가 다른 날짜 이면 사용자에게 경고하고 거부.
        # 어제 /tmp/brief_*.json 을 그대로 재사용하다 mixed row 만들었던 사고 재발 방지.
        flow_date = ((parsed_ctx.get("flow_today") or {}).get("date") or "").strip()
        if flow_date and flow_date != briefing_date:
            # 사용자가 명시적으로 의도한 케이스 (예: pre 가 어제 마감 데이터 참조) 는 허용.
            # 단 slot != 'pre' 이거나 차이가 7일 이상이면 stale 이라 거부.
            slot_lower = (args.slot or "").lower()
            allow_yesterday = slot_lower == "pre"  # pre 는 어제 데이터 정상
            try:
                from datetime import datetime as _dt
                diff_days = abs(
                    (_dt.strptime(briefing_date, "%Y-%m-%d") - _dt.strptime(flow_date, "%Y-%m-%d")).days
                )
            except Exception:
                diff_days = 999
            if (not allow_yesterday and diff_days >= 1) or diff_days > 7:
                msg = (
                    f"[ai_briefing_upsert] REJECT: context_json.flow_today.date={flow_date} "
                    f"!= briefing_date={briefing_date} (slot={args.slot}, diff={diff_days}d). "
                    f"새 context_json 을 작성하거나 --force 추가."
                )
                if not getattr(args, "force", False):
                    print(msg, file=sys.stderr)
                    print("[ai_briefing_upsert] hint: /tmp/brief_*.json 어제 파일 재사용 의심. 매 실행 매번 새로 만드세요.", file=sys.stderr)
                    return 3
                print(f"WARN: {msg} — --force 로 강행", file=sys.stderr)

        # 추가: indices.KOSPI.price 가 0 또는 None 이고 다른 indices 도 비었으면 의심
        indices = parsed_ctx.get("indices") or {}
        kospi_price = ((indices.get("KOSPI") or {}).get("price") or 0)
        if isinstance(kospi_price, (int, float)) and kospi_price == 0 and not getattr(args, "force", False):
            print(
                "[ai_briefing_upsert] WARN: indices.KOSPI.price=0 — 데이터 보류 마크인지 확인. "
                "고의면 --force 추가.",
                file=sys.stderr,
            )

    conn = get_stocks_conn()
    try:
        _ensure_table(conn)
        conn.execute(
            """
            INSERT INTO market_briefings
                (market, slot, briefing_date, slot_time, summary, context_json, model, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (market, briefing_date, slot) DO UPDATE SET
                slot_time = excluded.slot_time,
                summary = excluded.summary,
                context_json = excluded.context_json,
                model = excluded.model,
                created_at = excluded.created_at
            """,
            (
                args.market,
                args.slot,
                briefing_date,
                slot_time,
                summary,
                ctx_raw,
                MODEL_TAG,
                created,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    print(f"[ai_briefing_upsert] ok market={args.market} slot={args.slot} date={briefing_date} time={slot_time} summary_len={len(summary)}")

    # 푸시 자동 발송 (kind=briefing — 사용자별 시간 매트릭스 통과한 토큰에만)
    # 2026-05-11: 기본 OFF 로 전환. brief 작성 중 테스트로 푸시 가는 사고 차단.
    # 진짜 발송 원할 때만 BRIEF_PUSH_ENABLED=1 명시.
    if os.getenv("BRIEF_PUSH_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on"):
        try:
            _send_briefing_push(args.market, args.slot, summary, ctx_raw)
        except Exception as exc:
            print(f"[ai_briefing_upsert] push failed (non-fatal): {exc}", file=sys.stderr)

    return 0


def _send_briefing_push(market: str, slot: str, summary: str, ctx_raw):
    """브리핑 upsert 직후 _send_fcm_to_all 직접 호출. kind="briefing" 으로 보내
    NotificationPrefMatrix 의 시간 필터를 통과한 토큰에만 푸시.

    title: context_json.summary_structured.headline (없으면 summary 첫 줄)
    body:  summary_structured.closing 또는 summary 첫 두 줄
    url:   /brief/today
    """
    headline = ""
    closing = ""
    if ctx_raw:
        try:
            ctx = json.loads(ctx_raw)
            ss = ctx.get("summary_structured") or {}
            headline = str(ss.get("headline") or "").strip()
            closing = str(ss.get("closing") or "").strip()
        except Exception:
            pass

    if not headline:
        headline = (summary.splitlines() or [""])[0].strip()[:80]

    if not closing:
        body_lines = [ln.strip() for ln in summary.splitlines() if ln.strip()]
        closing = " ".join(body_lines[:2])[:200] if body_lines else headline

    slot_label = {
        "pre": "장전 브리핑",
        "morning": "오전 브리핑",
        "afternoon": "오후 브리핑",
        "post": "장마감 브리핑",
        "evening": "저녁 브리핑",
        "intra": "장중 브리핑",
    }.get(slot, "시황 브리핑")

    title = f"[{slot_label}] {headline}"[:120]
    body = closing[:240]

    payload = {
        "title": title,
        "body": body,
        "url": "/brief/today",
        "tag": f"briefing-{slot}",
        "kind": "briefing",
        "icon": "/icons/icon-notification.png",
    }

    try:
        from server.services.push_service import _send_fcm_to_all
    except Exception as exc:
        raise RuntimeError(f"push_service import failed: {exc}")

    res = _send_fcm_to_all(payload)
    print(f"[ai_briefing_upsert] push slot={slot} kind=briefing sent={res.get('sent')} reason={res.get('reason')}")


if __name__ == "__main__":
    sys.exit(main())
