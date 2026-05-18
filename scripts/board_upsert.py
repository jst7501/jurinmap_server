"""
종토방 민심 분석 **결과를 DB 에 기록**.

Claude Code 루틴이 종토방 컨텍스트를 읽고 스스로 `phase/score/contrarian/summary` 를
생성한 뒤 이 스크립트로 저장. 외부 AI API 없음. 발행 시각은 실행 시각으로 자동 기록.

사용법 (ok):
    python scripts/board_upsert.py \
      --code 000660 \
      --phase "환희/가즈아" \
      --score 85 \
      --summary "SK하이닉스 토론방은 HBM 독점 기대에 '가즈아' 열풍이에요. 단기 과열 신호로 관찰 필요해요." \
      --contrarian "고점 근접 신호 — 차익 실현 고려 국면. 신규 진입은 관망."

실패 기록:
    python scripts/board_upsert.py --code 123456 --status error --error "no_posts"

규칙:
- phase: 환희/가즈아 · 낙관 · 기대/의심 · 중립 · 현실부정/물타기 · 분노/원망 · 체념/자조 · 공포/패닉
        (프론트 classifySentiment 와 매핑되는 정확한 한국어 라벨)
- score: 0-100 정수. 50=중립, 80+ 극단 상승, 20- 극단 하락
- summary: 주린이 친화. 40-90자. `-요/-에요`. 매수·매도 권유 금지.
- contrarian: 한 줄 역발상 신호. 40-80자. "관망"·"관찰"·"참고" 허용.
- status: ok | error | no_data (기본 ok)
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


ALLOWED_PHASES = {
    "환희/가즈아", "낙관", "기대/의심", "중립",
    "현실부정/물타기", "분노/원망", "체념/자조", "공포/패닉",
    # 느슨하게 허용 (기존 v4 데이터 호환)
    "환희", "공포", "혼란",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True, help="6자리 종목 코드")
    ap.add_argument("--phase", default=None, help="감성 단계 한국어")
    ap.add_argument("--score", type=int, default=None, help="human_indicator_score 0-100")
    ap.add_argument("--summary", default=None, help="종토방 분위기 해설 (주린이)")
    ap.add_argument("--contrarian", default=None, help="역발상 신호 한 줄")
    ap.add_argument("--drivers", default=None, help="주요 키워드 콤마 구분 (issue_keywords)")
    ap.add_argument("--status", choices=["ok", "error", "no_data"], default="ok")
    ap.add_argument("--error", default=None, help="status != ok 일 때 원인")
    args = ap.parse_args()

    code = args.code.strip().zfill(6)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if args.status == "ok":
        if not args.phase or not args.summary:
            print("ERROR: --phase, --summary 필수 (status=ok)", file=sys.stderr)
            sys.exit(2)
        phase = args.phase.strip()
        if phase not in ALLOWED_PHASES:
            print(f"WARN: unknown phase '{phase}' — 저장은 진행, 프론트 매핑 확인 권장", file=sys.stderr)

    score = args.score if args.score is not None else 50
    if score < 0 or score > 100:
        score = max(0, min(100, score))

    issue_keywords = []
    if args.drivers:
        issue_keywords = [s.strip() for s in args.drivers.split(",") if s.strip()]

    # 에러·데이터없음 케이스: summary 에 이유 기록
    if args.status != "ok":
        phase = args.phase or "데이터 없음"
        summary = args.summary or f"종토방 분석 실패: {args.error or args.status}"
        contrarian = args.contrarian or "관망"
    else:
        phase = args.phase
        summary = args.summary
        contrarian = args.contrarian or "관망"

    conn = get_stocks_conn()
    try:
        # ① ai_analysis 테이블 — 종목 상세 페이지 ai_analysis 카드용 (요약·contrarian)
        conn.execute(
            """
            INSERT INTO ai_analysis(
                code, human_indicator_score, sentiment_phase, sentiment_phase_kor,
                core_issue, contrarian_signal, contrarian_signal_kor, summary,
                sentiment_keywords_json, issue_keywords_json, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                human_indicator_score=excluded.human_indicator_score,
                sentiment_phase=excluded.sentiment_phase,
                sentiment_phase_kor=excluded.sentiment_phase_kor,
                core_issue=excluded.core_issue,
                contrarian_signal=excluded.contrarian_signal,
                contrarian_signal_kor=excluded.contrarian_signal_kor,
                summary=excluded.summary,
                sentiment_keywords_json=excluded.sentiment_keywords_json,
                issue_keywords_json=excluded.issue_keywords_json,
                updated_at=excluded.updated_at
            """,
            (
                code,
                int(score),
                phase,              # sentiment_phase (영어 대신 한국어로 통일)
                phase,              # sentiment_phase_kor
                None,               # core_issue (미사용)
                contrarian,         # contrarian_signal (영어 대체 없이 한국어 동일)
                contrarian,         # contrarian_signal_kor
                summary,
                json.dumps([], ensure_ascii=False),               # sentiment_keywords_json (사용 안 함)
                json.dumps(issue_keywords, ensure_ascii=False),   # issue_keywords_json
                now,
            ),
        )

        # ② board_sentiment 테이블 — SentimentWidget · DivergenceSignalCard · 스크리너용
        # phase 한국어를 mood 로 그대로. score/grade/updated_at 만 정확하면 핵심 위젯 동작.
        # post_count·agree_ratio·top_euphoria/despair 는 board_context.py 가 계산하지만
        # 현재 SKILL 호출 인자에 없음 → 0/[] 로 두고, 풍부 인자는 별도 사이클에서 추가.
        conn.execute(
            """
            INSERT INTO board_sentiment(
                code, score, mood, grade, raw_score,
                post_count, agree_ratio, euphoria_count, despair_count,
                top_euphoria_json, top_despair_json, hot_posts_json, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                score=excluded.score,
                mood=excluded.mood,
                grade=excluded.grade,
                raw_score=excluded.raw_score,
                updated_at=excluded.updated_at
            """,
            (
                code,
                int(score),
                phase,                  # mood (한국어 phase 그대로)
                "분석 완료",            # grade (고정 라벨)
                float(score),           # raw_score
                0,                      # post_count (풍부 인자 추가 시 갱신)
                0,                      # agree_ratio
                0,                      # euphoria_count
                0,                      # despair_count
                json.dumps([], ensure_ascii=False),  # top_euphoria_json
                json.dumps([], ensure_ascii=False),  # top_despair_json
                json.dumps([], ensure_ascii=False),  # hot_posts_json
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    print(f"[ok] code={code} phase={phase} score={score} status={args.status}")


if __name__ == "__main__":
    main()
