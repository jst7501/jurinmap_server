"""
시황 브리핑 라우터 — /api/market-brief
─────────────────────────────────────────────
Claude scheduled task 가 하루 2회(07:55 장전 / 16:08 장마감 후) 써 넣는
`market_briefings` 테이블을 프론트에 노출.

엔드포인트:
  GET  /api/market-brief              일자별 그룹 (최신순, limit 기본 20)
  GET  /api/market-brief?market=KOSPI 시장 필터
  GET  /api/market-brief/latest       시장별 최신 1건 (홈 카드용)
  POST /api/market-brief              관리자/Claude 쓰기 (x-api-key)

스키마 (market_briefings):
  id, market, slot(pre|intra|post), briefing_date, slot_time,
  summary, context_json, model, created_at
PK/UNIQUE: (market, briefing_date, slot)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from ..db.connections import get_stocks_conn
from ..core.security import verify_http_request

logger = logging.getLogger("server.routes.market_brief")
router = APIRouter(prefix="/api/market-brief", tags=["market_brief"])


def _row_to_item(row) -> dict:
    ctx = None
    if row[6]:
        try:
            ctx = json.loads(row[6])
        except Exception:
            ctx = None
    return {
        "id": int(row[0]) if row[0] is not None else None,
        "market": row[1],
        "slot": row[2],
        "briefing_date": row[3],
        "slot_time": row[4],
        "summary": row[5] or "",
        "context": ctx,
        "model": row[7],
        "created_at": row[8],
    }


@router.get("")
def list_brief(market: Optional[str] = None, limit: int = 20):
    limit = max(1, min(int(limit or 20), 120))
    conn = get_stocks_conn()
    try:
        sql = (
            "SELECT id, market, slot, briefing_date, slot_time, summary, context_json, model, created_at "
            "FROM market_briefings "
        )
        params: tuple = ()
        if market:
            sql += "WHERE market = ? "
            params = (market.upper().strip(),)
        sql += "ORDER BY briefing_date DESC, slot_time DESC, id DESC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    groups: list[dict] = []
    current_date: Optional[str] = None
    current_bucket: Optional[dict] = None
    for r in rows:
        item = _row_to_item(r)
        d = item.get("briefing_date") or ""
        if d != current_date:
            if current_bucket is not None and len(groups) >= limit:
                break
            current_date = d
            current_bucket = {"date": d, "items": []}
            groups.append(current_bucket)
        current_bucket["items"].append(item)
    return {"groups": groups[:limit], "total_rows": len(rows)}


@router.get("/latest")
def latest_brief():
    """시장별 가장 최신 1건. 홈 카드용. (2026-04-30 롤백 — 시간대 select 제거)
    2026-05-11 추가: context_json 비어있는 row 는 skip — 화면 빈 헤드라인 증상 차단.
    """
    conn = get_stocks_conn()
    out: dict = {"KOSPI": None, "NASDAQ": None}
    try:
        for m in ("KOSPI", "NASDAQ"):
            # 최신 row 부터 최대 5개 보고 context_json 이 의미 있는 첫 row 채택.
            # ai_briefing_upsert 의 빈-ctx 가드를 우회한 옛 row 가 있어도 화면이 빈 헤드라인을 보여주지 않음.
            rows = conn.execute(
                "SELECT id, market, slot, briefing_date, slot_time, summary, context_json, model, created_at "
                "FROM market_briefings WHERE market=? "
                "ORDER BY briefing_date DESC, slot_time DESC, id DESC LIMIT 5",
                (m,),
            ).fetchall()
            for row in rows:
                ctx_raw = row[6] if not hasattr(row, "keys") else row["context_json"]
                # ctx 비어있거나 너무 작으면 skip (50 chars 미만은 의미 있는 brief 아님)
                if not ctx_raw or len(str(ctx_raw).strip()) < 50:
                    continue
                out[m] = _row_to_item(row)
                break
    finally:
        conn.close()
    return out


@router.post("")
async def create_brief(request: Request):
    """Claude scheduled task / 관리자 수동 쓰기.
    Body: {market, slot, summary, briefing_date?, slot_time?, context_json?, model?}
    """
    ok, reason = verify_http_request(request)
    if not ok:
        raise HTTPException(401, detail=reason or "unauthorized")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "body must be JSON object")

    summary = str(payload.get("summary") or "").strip()
    if not summary:
        raise HTTPException(400, "summary required")

    market = str(payload.get("market") or "KOSPI").upper().strip()
    slot = str(payload.get("slot") or "post").lower().strip()
    # 2026-04-24: slot 확장 — 5단계 시간대 (pre/morning/afternoon/post/evening)
    # intra 는 legacy 지원만.
    ALLOWED_SLOTS = ("pre", "morning", "afternoon", "post", "evening", "intra")
    if slot not in ALLOWED_SLOTS:
        raise HTTPException(400, f"slot must be one of {'|'.join(ALLOWED_SLOTS)}")

    now = datetime.now()
    briefing_date = str(payload.get("briefing_date") or now.strftime("%Y-%m-%d")).strip()
    slot_time = str(payload.get("slot_time") or now.strftime("%H:%M")).strip()
    created_at = now.strftime("%Y-%m-%d %H:%M:%S")

    context_json = payload.get("context_json")
    if context_json is not None and not isinstance(context_json, str):
        context_json = json.dumps(context_json, ensure_ascii=False)

    model = payload.get("model") or "claude-agent"

    conn = get_stocks_conn()
    try:
        conn.execute(
            """
            INSERT INTO market_briefings(market, slot, briefing_date, slot_time, summary, context_json, model, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market, briefing_date, slot) DO UPDATE SET
              slot_time=excluded.slot_time,
              summary=excluded.summary,
              context_json=excluded.context_json,
              model=excluded.model,
              created_at=excluded.created_at
            """,
            (market, slot, briefing_date, slot_time, summary, context_json, model, created_at),
        )
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "market": market, "slot": slot, "date": briefing_date}
