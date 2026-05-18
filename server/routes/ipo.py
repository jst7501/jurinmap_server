from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from server.services.ipo_service import IpoService, IpoServiceError

router = APIRouter()
_ipo_service = IpoService()


@router.get("/api/ipo/status")
def ipo_status():
    return _ipo_service.status()


@router.get("/api/ipo/events")
async def ipo_events(
    refresh: bool = Query(default=False),
    days: int = Query(default=90, ge=1, le=90),
    page_count: int = Query(default=100, ge=10, le=100),
):
    try:
        return await _ipo_service.get_events(refresh=refresh, days=days, page_count=page_count)
    except IpoServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/api/ipo/refresh")
async def ipo_refresh(
    days: int = Query(default=90, ge=1, le=90),
    page_count: int = Query(default=100, ge=10, le=100),
):
    try:
        return await _ipo_service.fetch_and_cache(days=days, page_count=page_count)
    except IpoServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/api/ipo/guide")
def ipo_guide():
    return _ipo_service.get_beginner_guide()
