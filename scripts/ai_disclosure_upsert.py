"""
AI 공시 임팩트 루틴 **결과를 DB 에 기록**.

Claude Code 루틴이 각 공시(rcept_no) 마다 스스로 임팩트 분석을 생성한 뒤
이 스크립트로 dart_disclosures 의 title_kor / summary_kor / impact / release_eta
컬럼을 UPDATE 한다. 새 테이블 생성 X — 기존 컬럼 채우는 패턴.

사용법:
    python scripts/ai_disclosure_upsert.py \
      --rcept-no 20260520000123 \
      --title-kor "유상증자 결정 (3,000억원, 주주배정 후 일반공모)" \
      --summary-kor "기존 주주 지분이 약 8% 희석되고, 조달 자금은 2차전지 라인 증설에 쓰여요." \
      --impact negative --strength 3 \
      --release-eta "2026-07-15"

실패 기록:
    python scripts/ai_disclosure_upsert.py --rcept-no 20260520000123 \
      --status error --error "body_unavailable"

규칙:
- impact 는 positive | negative | neutral | risk 중 하나 (방향)
  - positive: 주가에 호재 (수주·실적상향·자사주 매입/소각·무상증자·흑자전환 등)
  - negative: 주가에 악재 (유상증자·CB/BW·전환사채·실적하향)
  - neutral:  중립 또는 영향 미미 (분기보고서·ELS 발행·일상 공시)
  - risk:     주의 (불성실공시·관리종목·거래정지·감사의견 거절·횡령배임)

- strength 는 1 | 2 | 3 (강도 — 호재/악재가 '얼마나 센가'). neutral 은 보통 생략(1).
  판단 신호: 공시 type + (금액 ÷ 시가총액) + 5일 주가/수급 반응
  - 3 (강): 대규모 공급계약(매출比 큰%)·자사주 소각·무상증자·흑자전환·신약 허가·어닝 서프라이즈
            / 대규모 유상증자(큰 희석)·CB·BW·횡령배임·감사의견 거절·상장폐지 심사·자본잠식
  - 2 (중): 양호한 실적·중형 수주·자사주 취득 / 실적 부진·관리종목 우려·소송
  - 1 (약): 통상 계약·소규모 IR / 경미한 정정·소액 소송. neutral 공시도 1.

- title_kor 25-60 자 권장, summary_kor 40-120 자 권장 (주린이 톤)
- 이모지 금지. 매수/매도/사세요/팔아야 금지.
- release_eta 는 'YYYY-MM-DD' 또는 None (행사 일정 있는 공시만)
- translated_at 은 자동으로 NOW() 기록
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.db.connections import get_stocks_conn  # noqa: E402


VALID_IMPACTS = {"positive", "negative", "neutral", "risk"}
VALID_STRENGTHS = {1, 2, 3}
VALID_STATUSES = {"ok", "error", "no_data"}


def _now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def upsert_disclosure_impact(
    con,
    rcept_no: str,
    title_kor: str | None,
    summary_kor: str | None,
    impact: str | None,
    strength: int | None,
    release_eta: str | None,
    status: str,
    error: str | None,
):
    """dart_disclosures UPDATE — rcept_no 기준."""
    cur = con.cursor()
    # 행 존재 확인
    row = cur.execute(
        "SELECT id FROM dart_disclosures WHERE rcept_no = %s",
        (rcept_no,),
    ).fetchone()
    if not row:
        raise SystemExit(f"rcept_no not found in dart_disclosures: {rcept_no}")

    if status != "ok":
        # 실패는 created_at 가 아닌 별도 필드가 없으므로 title_kor 에 마커 남기지 않음.
        # 추후 ai_disclosure_failures 테이블 분리 가능. 일단 translated_at 만 NOW().
        cur.execute(
            """
            UPDATE dart_disclosures
            SET translated_at = NOW()
            WHERE rcept_no = %s
            """,
            (rcept_no,),
        )
        print(f"[ok] {rcept_no} status={status} error={error or '-'} (translated_at marked)")
        return

    if impact and impact not in VALID_IMPACTS:
        raise SystemExit(f"invalid impact={impact} (valid: {sorted(VALID_IMPACTS)})")
    if strength is not None and strength not in VALID_STRENGTHS:
        raise SystemExit(f"invalid strength={strength} (valid: {sorted(VALID_STRENGTHS)})")

    cur.execute(
        """
        UPDATE dart_disclosures
        SET title_kor       = COALESCE(%s, title_kor),
            summary_kor     = COALESCE(%s, summary_kor),
            impact          = COALESCE(%s, impact),
            impact_strength = COALESCE(%s, impact_strength),
            release_eta     = COALESCE(%s, release_eta),
            translated_at   = NOW()
        WHERE rcept_no = %s
        """,
        (title_kor, summary_kor, impact, strength, release_eta, rcept_no),
    )
    print(
        f"[ok] {rcept_no} impact={impact or '-'}/{strength or '-'} "
        f"title_kor={(title_kor or '')[:30]!r}"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rcept-no", required=True)
    ap.add_argument("--title-kor", default=None)
    ap.add_argument("--summary-kor", default=None)
    ap.add_argument("--impact", choices=sorted(VALID_IMPACTS), default=None)
    ap.add_argument("--strength", type=int, choices=sorted(VALID_STRENGTHS), default=None,
                    help="1(약)|2(중)|3(강) — 호재/악재 강도")
    ap.add_argument("--release-eta", default=None,
                    help="YYYY-MM-DD 또는 빈값 (행사 일정 있는 공시만)")
    ap.add_argument("--status", choices=sorted(VALID_STATUSES), default="ok")
    ap.add_argument("--error", default=None)
    args = ap.parse_args()

    if args.status == "ok" and not (args.title_kor or args.summary_kor or args.impact):
        raise SystemExit("status=ok 일 때 title-kor/summary-kor/impact 중 최소 1개 필요")

    con = get_stocks_conn()
    try:
        upsert_disclosure_impact(
            con,
            rcept_no=args.rcept_no,
            title_kor=args.title_kor,
            summary_kor=args.summary_kor,
            impact=args.impact,
            strength=args.strength,
            release_eta=args.release_eta,
            status=args.status,
            error=args.error,
        )
        con.commit()
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
