"""투자경고 / 공시 (DART) 풀이 라우트.

엔드포인트:
- GET /api/warnings/list                       — 현재 투자주의/경고/위험/거래정지 전체
- GET /api/stocks/{code}/disclosures?limit=N    — 종목 최근 공시 (번역 포함)
- GET /api/disclosures/pending?codes=...       — 미번역 공시 batch (Claude 번역 작업용)
- POST /api/disclosures/translate              — 번역 결과 batch upsert

투자경고 reason 풀이는 정적 매핑 (WARNING_GUIDE) + reason 텍스트.
공시 번역은 Claude Code 가 직접 작성 (CLAUDE.md 정책 — 외부 AI API 금지).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query

from server.db.connections import get_stocks_conn

router = APIRouter(prefix="/api", tags=["disclosures"])
logger = logging.getLogger("server.routes.disclosures")


# ─── 투자경고 풀이 (정적 매핑) ───────────────────────────────
WARNING_GUIDE = {
    "caution": {
        "label": "투자주의",
        "color": "amber",
        "explanation": "최근 단기 급등이나 비정상 거래 패턴이 감지된 종목이에요. 매매 전 신중하게 판단하세요.",
        "release_rule": "다음 매매거래일까지 추가 이상 신호가 없으면 자동 해제돼요.",
    },
    "warning": {
        "label": "투자경고",
        "color": "orange",
        "explanation": "투자주의 위 단계예요. 단기간 급등(예: 5일 60%↑)이 있어 거래소가 위험 신호를 띄운 상태. 추가 급등 시 거래정지로 갈 수 있어요.",
        "release_rule": "지정 후 10영업일 이상 + 추가 급등 없을 때 해제 검토해요.",
    },
    "risk": {
        "label": "투자위험",
        "color": "red",
        "explanation": "가장 높은 단계. 거래정지가 곧바로 따라올 수 있고 변동성이 극단적이에요.",
        "release_rule": "지정 후 추가 이상 신호 없이 일정 기간 경과해야 해제.",
    },
    "trading_halt": {
        "label": "거래정지",
        "color": "red",
        "explanation": "거래 자체가 멈춘 상태예요. 사유에 따라 풀리는 시점이 달라요.",
        "release_rule": "사유별 다름. 아래 reason 칸의 사유를 보고 판단해야 해요.",
    },
}

# 거래정지 사유별 풀이 (자주 나오는 패턴 기반)
HALT_REASON_GUIDE = [
    ("상장폐지 사유발생",      "정밀 심사 후 결정. 보통 수개월 걸려요. 최악엔 상장폐지로 갈 수 있어 매수는 매우 신중."),
    ("주식의 병합",            "주식수 조정(액면합병 등)이 끝나면 신주 상장 기준일에 거래 재개. 보통 1-2주."),
    ("주식의 분할",            "액면분할 처리 끝나면 신주 상장 기준일에 재개. 보통 1-2주."),
    ("전자등록 변경",          "예탁원 등록 변경 작업. 보통 1-2주 안 재개."),
    ("말소",                  "전자등록 말소 절차. 보통 1-2주."),
    ("투자경고 및 위험",       "단기 급등으로 인한 매매거래정지. 1거래일 정지 후 재개되는 경우가 많아요."),
    ("관리종목",              "재무 부실·감사의견 거절 등 관리종목 지정. 사유 해소까지 정지 가능."),
    ("불성실공시",            "공시 위반으로 정지. 통상 1거래일."),
    ("기타",                  "공시 사유 텍스트 그대로 확인 필요."),
]


def _explain_halt_reason(reason: str) -> str:
    if not reason:
        return HALT_REASON_GUIDE[-1][1]
    for keyword, explain in HALT_REASON_GUIDE:
        if keyword in reason:
            return explain
    return HALT_REASON_GUIDE[-1][1]


# warning_type 별 DART 공시 title 매칭 키워드
_WARNING_TITLE_KEYWORDS = {
    "caution":      ["투자주의", "단기과열", "스팸관여", "소수계좌"],
    "warning":      ["투자경고"],
    "risk":         ["투자위험"],
    "trading_halt": ["거래정지", "매매거래정지", "매매정지"],
}


def _find_warning_disclosure(conn, code: str, warning_type: str):
    """warning_type 라벨 키워드가 title 에 포함된 dart_disclosures 1건 반환.

    같은 종목의 dart_disclosures 중 warning_type 매칭 키워드가 title 에 있는
    가장 최근 공시 1건. 번역(title_kor 또는 summary_kor)이 있는 것만 반환.
    매칭 없으면 None.
    """
    keywords = _WARNING_TITLE_KEYWORDS.get(warning_type, [])
    if not keywords or not code:
        return None
    title_clauses = " OR ".join(["title LIKE ?" for _ in keywords])
    params = [code] + [f"%{kw}%" for kw in keywords]
    rows = conn.execute(
        f"""
        SELECT id, rcept_no, date, title, title_kor, summary_kor, impact, release_eta
        FROM dart_disclosures
        WHERE code = ? AND ({title_clauses})
        ORDER BY date DESC, id DESC
        LIMIT 1
        """,
        params,
    ).fetchall()
    if not rows:
        return None
    r = dict(rows[0])
    if not (r.get("title_kor") or r.get("summary_kor")):
        return None
    return {
        "id": r.get("id"),
        "rcept_no": r.get("rcept_no"),
        "date": str(r.get("date") or ""),
        "title": r.get("title"),
        "title_kor": r.get("title_kor"),
        "summary_kor": r.get("summary_kor"),
        "impact": r.get("impact"),
        "release_eta": r.get("release_eta"),
    }


@router.get("/warnings/list")
def list_warnings(warning_type: Optional[str] = Query(None, description="caution|warning|risk|trading_halt|all")):
    """현재 투자경고/주의/위험/거래정지 종목 전체 목록 + 정적 풀이."""
    conn = get_stocks_conn()
    try:
        if warning_type and warning_type != "all":
            rows = conn.execute(
                """
                SELECT iw.code, s.name, s.market,
                       iw.warning_type, iw.designated_date, iw.reason, iw.note,
                       iw.updated_at,
                       pt.current_price, pt.change_pct, pt.market_cap
                FROM investment_warnings iw
                LEFT JOIN stocks s     ON s.code = iw.code
                LEFT JOIN price_today pt ON pt.code = iw.code
                WHERE iw.warning_type = ?
                ORDER BY iw.designated_date DESC, iw.code ASC
                """,
                (warning_type,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT iw.code, s.name, s.market,
                       iw.warning_type, iw.designated_date, iw.reason, iw.note,
                       iw.updated_at,
                       pt.current_price, pt.change_pct, pt.market_cap
                FROM investment_warnings iw
                LEFT JOIN stocks s     ON s.code = iw.code
                LEFT JOIN price_today pt ON pt.code = iw.code
                ORDER BY iw.designated_date DESC, iw.code ASC
                """
            ).fetchall()
    finally:
        conn.close()

    items = []
    for r in rows:
        d = dict(r) if hasattr(r, "keys") else {}
        wt = d.get("warning_type") or "trading_halt"
        guide = WARNING_GUIDE.get(wt, WARNING_GUIDE["trading_halt"])
        item = {
            "code": d.get("code"),
            "name": d.get("name"),
            "market": d.get("market"),
            "warning_type": wt,
            "label": guide["label"],
            "color": guide["color"],
            "explanation": guide["explanation"],
            "release_rule": guide["release_rule"],
            "designated_date": str(d.get("designated_date") or ""),
            "reason": d.get("reason"),
            "note": d.get("note"),
            "updated_at": str(d.get("updated_at") or ""),
            "current_price": d.get("current_price"),
            "change_pct": d.get("change_pct"),
            "market_cap": d.get("market_cap"),
        }
        if wt == "trading_halt":
            item["reason_explanation"] = _explain_halt_reason(d.get("reason") or "")
        items.append(item)

    # 종류별 카운트
    counts = {"caution": 0, "warning": 0, "risk": 0, "trading_halt": 0}
    for it in items:
        wt = it.get("warning_type")
        if wt in counts:
            counts[wt] += 1

    return {
        "total": len(items),
        "counts": counts,
        "items": items,
        "guide": WARNING_GUIDE,
        "fetched_at": datetime.now().isoformat(),
    }


@router.get("/stocks/{code}/disclosures")
def get_stock_disclosures(code: str, limit: int = Query(10, ge=1, le=50)):
    """종목 최근 공시 + 번역 + 투자경고 지정 현황.

    응답:
    {
      "code": "005930",
      "warnings": [    // 투자주의/경고/위험/거래정지 지정 현황 (없으면 빈 배열)
        {
          "warning_type": "warning",
          "label": "투자경고",
          "color": "orange",
          "explanation": "...",
          "release_rule": "...",
          "designated_date": "20260507",
          "reason": "...",
          "reason_explanation": "..."   // 거래정지 사유 풀이 (trading_halt 만)
        }
      ],
      "items": [
        {id, rcept_no, date, title, type, title_kor, summary_kor, impact, release_eta, translated, ...}
      ]
    }
    """
    if not code or len(code) != 6 or not code.isdigit():
        raise HTTPException(400, "invalid code")

    conn = get_stocks_conn()
    try:
        # 1) 종목별 투자경고 지정 현황 (한 종목에 여러 지정 동시 가능)
        warning_rows = conn.execute(
            """
            SELECT warning_type, designated_date, reason, note, updated_at
            FROM investment_warnings
            WHERE code = ?
            ORDER BY designated_date DESC
            """,
            (code,),
        ).fetchall()

        # 2) 최근 공시
        rows = conn.execute(
            """
            SELECT id, rcept_no, date, title, type,
                   title_kor, summary_kor, impact, release_eta, translated_at,
                   created_at
            FROM dart_disclosures
            WHERE code = ?
            ORDER BY date DESC, id DESC
            LIMIT ?
            """,
            (code, limit),
        ).fetchall()

        # 3) 각 warning 마다 매칭되는 거래소 공시 1건 (title 키워드 매칭)
        warnings = []
        for r in warning_rows:
            d = dict(r) if hasattr(r, "keys") else {}
            wt = d.get("warning_type") or "trading_halt"
            designated_date = str(d.get("designated_date") or "").strip()
            reason = (d.get("reason") or "").strip()

            # 매칭 공시 검색 — designated_date/reason 비어있어도 시도
            linked = _find_warning_disclosure(conn, code, wt)

            # 데이터 없는 빈 행 skip — designated_date/reason/공시 모두 없으면 화면에 표시할 정보 0
            if not designated_date and not reason and not linked:
                continue

            guide = WARNING_GUIDE.get(wt, WARNING_GUIDE["trading_halt"])
            w = {
                "warning_type": wt,
                "label": guide["label"],
                "color": guide["color"],
                "explanation": guide["explanation"],
                "release_rule": guide["release_rule"],
                "designated_date": designated_date,
                "reason": reason or None,
                "note": d.get("note"),
                "updated_at": str(d.get("updated_at") or ""),
                # 거래소가 발행한 매칭 공시 (있으면) — 프론트에서 summary_kor 표시용
                "disclosure": linked,
            }
            if wt == "trading_halt":
                w["reason_explanation"] = _explain_halt_reason(reason)
            warnings.append(w)
    finally:
        conn.close()

    items = []
    for r in rows:
        d = dict(r) if hasattr(r, "keys") else {}
        items.append({
            "id": d.get("id"),
            "rcept_no": d.get("rcept_no"),
            "date": str(d.get("date") or ""),
            "title": d.get("title"),
            "type": d.get("type"),
            "title_kor": d.get("title_kor"),
            "summary_kor": d.get("summary_kor"),
            "impact": d.get("impact"),
            "release_eta": d.get("release_eta"),
            "translated": d.get("translated_at") is not None,
            "translated_at": str(d.get("translated_at") or "") or None,
            "created_at": str(d.get("created_at") or "") or None,
        })

    return {
        "code": code,
        "warnings": warnings,
        "count": len(items),
        "items": items,
    }


@router.get("/disclosures/pending")
def list_pending_disclosures(
    codes: Optional[str] = Query(None, description="콤마 분리 종목 코드 list — 비우면 전체"),
    limit: int = Query(200, ge=1, le=2000),
    days: int = Query(7, ge=1, le=60, description="최근 N일치 공시"),
):
    """미번역 공시 batch 조회 — Claude 가 번역 batch 작업 시 호출.

    번역 안 된(`translated_at IS NULL`) 행만 반환. 같은 종목 내 최신순.
    """
    code_list: list[str] = []
    if codes:
        for c in codes.split(","):
            c = c.strip()
            if c and c.isdigit() and len(c) == 6:
                code_list.append(c)

    conn = get_stocks_conn()
    try:
        if code_list:
            placeholders = ",".join("?" * len(code_list))
            sql = f"""
                SELECT d.id, d.code, s.name, d.date, d.title, d.type, d.rcept_no
                FROM dart_disclosures d
                LEFT JOIN stocks s ON s.code = d.code
                WHERE d.translated_at IS NULL
                  AND d.code IN ({placeholders})
                  AND d.date >= TO_CHAR(CURRENT_DATE - INTERVAL '{days} days', 'YYYYMMDD')
                ORDER BY d.date DESC, d.id DESC
                LIMIT ?
            """
            rows = conn.execute(sql, code_list + [limit]).fetchall()
        else:
            sql = f"""
                SELECT d.id, d.code, s.name, d.date, d.title, d.type, d.rcept_no
                FROM dart_disclosures d
                LEFT JOIN stocks s ON s.code = d.code
                WHERE d.translated_at IS NULL
                  AND d.date >= TO_CHAR(CURRENT_DATE - INTERVAL '{days} days', 'YYYYMMDD')
                ORDER BY d.date DESC, d.id DESC
                LIMIT ?
            """
            rows = conn.execute(sql, (limit,)).fetchall()
    finally:
        conn.close()

    items = []
    for r in rows:
        d = dict(r) if hasattr(r, "keys") else {}
        items.append({
            "id": d.get("id"),
            "code": d.get("code"),
            "name": d.get("name"),
            "date": str(d.get("date") or ""),
            "title": d.get("title"),
            "type": d.get("type"),
            "rcept_no": d.get("rcept_no"),
        })

    return {
        "count": len(items),
        "items": items,
        "scope": {"codes": code_list or "all", "limit": limit, "days": days},
    }


@router.post("/disclosures/translate")
def upsert_translations(payload: dict = Body(...)):
    """번역 결과 batch upsert. Claude(메인/서브에이전트) 가 호출.

    payload:
    {
      "items": [
        {
          "id": 123,
          "title_kor": "한글 풀이",
          "summary_kor": "한 줄 요약",
          "impact": "positive|neutral|negative|risk",
          "release_eta": "효과 시점 (선택)"
        }, ...
      ]
    }
    """
    items = (payload or {}).get("items") or []
    if not isinstance(items, list) or not items:
        raise HTTPException(400, "items array required")

    valid_impacts = {"positive", "neutral", "negative", "risk"}
    now_ts = datetime.now()
    written = 0
    skipped = 0

    conn = get_stocks_conn()
    try:
        for it in items:
            try:
                row_id = int(it.get("id"))
            except (TypeError, ValueError):
                skipped += 1
                continue
            title_kor = (it.get("title_kor") or "").strip() or None
            summary_kor = (it.get("summary_kor") or "").strip() or None
            impact = (it.get("impact") or "").strip().lower()
            if impact not in valid_impacts:
                impact = None
            release_eta = (it.get("release_eta") or "").strip() or None

            if not (title_kor or summary_kor):
                skipped += 1
                continue

            conn.execute(
                """
                UPDATE dart_disclosures
                SET title_kor = ?,
                    summary_kor = ?,
                    impact = ?,
                    release_eta = ?,
                    translated_at = ?
                WHERE id = ?
                """,
                (title_kor, summary_kor, impact, release_eta, now_ts, row_id),
            )
            written += 1
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "written": written, "skipped": skipped, "translated_at": now_ts.isoformat()}
