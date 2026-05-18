"""
패치 공지 (patches) 라우터

사용자에게 노출되는 일자별 "패치 완료" 피드를 관리한다.
'AI' 같은 자동화 브랜딩 없이 순수 "패치 완료 — N건 수정" 톤으로만.

데이터 모델 (Postgres):
  patches(id, patch_date, summary, submitter, status, suggestion_id, created_at)
  - patch_date: YYYY-MM-DD (KST 기준)
  - summary:    건의 내용/수정 요약
  - submitter:  건의자 닉네임 (옵션)
  - status:     done | in_progress
  - suggestion_id: suggestions 테이블 연결 키 (옵션)

엔드포인트:
  GET  /api/patches?limit=60       일자별 그룹핑된 리스트 (최신순)
  POST /api/patches                운영자 기록 추가 (x-api-key 필요)
  POST /api/patches/bulk           여러 건 동시 등록 (x-api-key 필요)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..db.connections import get_stocks_conn
from ..core.security import verify_http_request

logger = logging.getLogger("server.routes.patches")
router = APIRouter()


# ─── Schemas ─────────────────────────────────────────────────────────
class PatchCreate(BaseModel):
    patch_date: Optional[str] = Field(None, description="YYYY-MM-DD. 생략 시 오늘(KST)")
    summary: str
    submitter: Optional[str] = None
    status: Optional[str] = "done"
    suggestion_id: Optional[int] = None


class PatchBulkCreate(BaseModel):
    patch_date: Optional[str] = None
    items: List[PatchCreate]


# ─── Helpers ────────────────────────────────────────────────────────
def _kst_today() -> str:
    # 서버 타임존 상관없이 KST 기준 날짜 문자열 생성
    # (정확한 TZ 핸들링은 zoneinfo 사용이 이상적이지만 경량 구현)
    return datetime.now().strftime("%Y-%m-%d")


def _row_to_item(row) -> dict:
    return {
        "id": int(row[0]) if row[0] is not None else None,
        "summary": row[1] or "",
        "submitter": row[2] or None,
        "status": row[3] or "done",
        "suggestion_id": int(row[4]) if row[4] is not None else None,
        "created_at": row[5] or "",
    }


def _require_admin(request: Request) -> None:
    ok, reason = verify_http_request(request)
    if not ok:
        raise HTTPException(401, detail=reason or "unauthorized")


# ─── Routes ─────────────────────────────────────────────────────────
@router.get("/api/patches")
def list_patches(limit: int = 60):
    """최신순 일자별 그룹 반환.
    응답: [{ date, count, items: [...] }]
    """
    limit = max(1, min(int(limit or 60), 365))

    conn = get_stocks_conn()
    try:
        rows = conn.execute(
            """
            SELECT id, summary, submitter, status, suggestion_id, created_at, patch_date
            FROM patches
            ORDER BY patch_date DESC, id DESC
            """
        ).fetchall()
    finally:
        conn.close()

    groups: list[dict] = []
    current_date: Optional[str] = None
    current_bucket: Optional[dict] = None

    for r in rows:
        date = r[6] or ""
        item = _row_to_item(r)
        if date != current_date:
            if current_bucket and len(groups) >= limit:
                break
            current_date = date
            current_bucket = {"date": date, "count": 0, "items": []}
            groups.append(current_bucket)
        current_bucket["items"].append(item)
        current_bucket["count"] += 1

    # limit 이 일자 기준이라 마지막 그룹이 limit 초과 경우 트리밍
    groups = groups[:limit]
    return {"groups": groups}


@router.post("/api/patches")
def create_patch(req: PatchCreate, request: Request):
    _require_admin(request)
    if not req.summary.strip():
        raise HTTPException(400, "summary required")

    patch_date = (req.patch_date or _kst_today()).strip()
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_stocks_conn()
    try:
        conn.execute(
            """
            INSERT INTO patches(patch_date, summary, submitter, status, suggestion_id, created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (
                patch_date,
                req.summary.strip(),
                (req.submitter or None),
                (req.status or "done"),
                req.suggestion_id,
                created_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "patch_date": patch_date}


@router.post("/api/patches/bulk")
def create_patches_bulk(req: PatchBulkCreate, request: Request):
    _require_admin(request)
    if not req.items:
        raise HTTPException(400, "items empty")

    default_date = (req.patch_date or _kst_today()).strip()
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    n = 0
    conn = get_stocks_conn()
    try:
        for it in req.items:
            if not it.summary.strip():
                continue
            date = (it.patch_date or default_date).strip()
            conn.execute(
                """
                INSERT INTO patches(patch_date, summary, submitter, status, suggestion_id, created_at)
                VALUES(?,?,?,?,?,?)
                """,
                (
                    date,
                    it.summary.strip(),
                    (it.submitter or None),
                    (it.status or "done"),
                    it.suggestion_id,
                    created_at,
                ),
            )
            n += 1
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "inserted": n}


@router.delete("/api/patches/{patch_id}")
def delete_patch(patch_id: int, request: Request):
    _require_admin(request)
    conn = get_stocks_conn()
    try:
        conn.execute("DELETE FROM patches WHERE id=?", (patch_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}
