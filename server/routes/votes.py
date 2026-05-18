"""
유저 투표 라우터
──────────────────────────────────────────────
두 가지 투표 API 제공:

1) 코스피 UP/DOWN 예측 투표
   POST /api/kospi-prediction       {target_date?, prediction:'up'|'down'}
   GET  /api/kospi-prediction       ?target_date=YYYY-MM-DD
   - target_date 기본: 다음 거래일(단순히 오늘+1)
   - voter_id 는 헤더 X-Voter-Id (닉네임 기반) 또는 쿠키 fingerprint
   - 같은 voter가 같은 target_date에 재투표하면 갱신

2) 뉴스 호재/중립/악재 투표 (뉴스 카드 인라인용)
   POST /api/news/{news_id}/vote    {vote:'good'|'neutral'|'bad'}
   GET  /api/news/{news_id}/vote
   - news_id 는 news_events.id 또는 외부 고유 키
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..db.connections import get_stocks_conn

logger = logging.getLogger("server.routes.votes")
router = APIRouter()


# ─── 공통 ──────────────────────────────────────────────────────────
def _now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _voter_id(request: Request, supplied: Optional[str] = None) -> str:
    """닉네임(X-Voter-Id) 우선, 없으면 쿠키/IP 지문 fallback.
    투표 중복 방지와 '참여자 수' 집계에 쓰임.
    """
    candidates = [
        supplied,
        request.headers.get("x-voter-id"),
        request.headers.get("x-nickname"),
    ]
    for c in candidates:
        if c and str(c).strip():
            return str(c).strip()[:64]

    # fallback: IP + UA 조합 (정확도 낮음, 비정상 투표 소량 허용)
    ip = (request.client.host if request.client else "?") or "?"
    ua = str(request.headers.get("user-agent") or "")[:40]
    return f"anon:{ip}:{hash(ua) & 0xffff:04x}"


# ─── Schemas ────────────────────────────────────────────────────────
class KospiPredictionIn(BaseModel):
    target_date: Optional[str] = None
    prediction: Literal["up", "down"]
    voter_id: Optional[str] = Field(None, description="닉네임 우선. 없으면 헤더 X-Voter-Id 또는 익명 지문")


class NewsVoteIn(BaseModel):
    vote: Literal["good", "neutral", "bad"]
    voter_id: Optional[str] = None


# ─── 코스피 예측 ────────────────────────────────────────────────────
def _default_target_date() -> str:
    """다음 거래일 단순 계산: 토·일은 월요일로."""
    t = datetime.now()
    # 한국 장 마감(15:30) 이후면 내일 대상, 전이면 오늘 대상
    if t.hour < 15 or (t.hour == 15 and t.minute < 30):
        target = t
    else:
        target = t + timedelta(days=1)
    # 주말 skip
    while target.weekday() >= 5:
        target = target + timedelta(days=1)
    return target.strftime("%Y-%m-%d")


@router.get("/api/kospi-prediction")
def kospi_prediction_summary(request: Request, target_date: Optional[str] = None):
    date = (target_date or _default_target_date()).strip()
    voter = _voter_id(request)
    conn = get_stocks_conn()
    try:
        rows = conn.execute(
            "SELECT prediction, COUNT(*) FROM kospi_predictions WHERE target_date=? GROUP BY prediction",
            (date,),
        ).fetchall()
        counts = {"up": 0, "down": 0}
        for r in rows:
            key = str(r[0] or "").lower()
            if key in counts:
                counts[key] = int(r[1])
        total = counts["up"] + counts["down"]

        my_row = conn.execute(
            "SELECT prediction FROM kospi_predictions WHERE target_date=? AND voter_id=?",
            (date, voter),
        ).fetchone()
        my_vote = (my_row[0] if my_row else None)
    finally:
        conn.close()

    up_pct = round((counts["up"] / total) * 100, 1) if total else 0.0
    down_pct = round((counts["down"] / total) * 100, 1) if total else 0.0
    return {
        "target_date": date,
        "total": total,
        "up": counts["up"],
        "down": counts["down"],
        "up_pct": up_pct,
        "down_pct": down_pct,
        "my_vote": my_vote,
    }


@router.post("/api/kospi-prediction")
def kospi_prediction_cast(req: KospiPredictionIn, request: Request):
    date = (req.target_date or _default_target_date()).strip()
    voter = _voter_id(request, req.voter_id)
    ts = _now_ts()

    conn = get_stocks_conn()
    try:
        conn.execute(
            """
            INSERT INTO kospi_predictions(target_date, voter_id, prediction, created_at)
            VALUES(?,?,?,?)
            ON CONFLICT(target_date, voter_id) DO UPDATE SET
              prediction=excluded.prediction,
              created_at=excluded.created_at
            """,
            (date, voter, req.prediction, ts),
        )
        conn.commit()
    finally:
        conn.close()

    # 투표 후 갱신된 집계를 함께 반환 (UI 즉시 반영용)
    return kospi_prediction_summary(request, target_date=date)


# ─── 뉴스 투표 ──────────────────────────────────────────────────────
@router.get("/api/news/{news_id}/vote")
def news_vote_summary(news_id: str, request: Request):
    voter = _voter_id(request)
    conn = get_stocks_conn()
    try:
        rows = conn.execute(
            "SELECT vote, COUNT(*) FROM news_votes WHERE news_id=? GROUP BY vote",
            (news_id,),
        ).fetchall()
        counts = {"good": 0, "neutral": 0, "bad": 0}
        for r in rows:
            key = str(r[0] or "").lower()
            if key in counts:
                counts[key] = int(r[1])
        my_row = conn.execute(
            "SELECT vote FROM news_votes WHERE news_id=? AND voter_id=?",
            (news_id, voter),
        ).fetchone()
        my_vote = (my_row[0] if my_row else None)
    finally:
        conn.close()
    return {
        "news_id": news_id,
        "good": counts["good"],
        "neutral": counts["neutral"],
        "bad": counts["bad"],
        "total": counts["good"] + counts["neutral"] + counts["bad"],
        "my_vote": my_vote,
    }


@router.post("/api/news/{news_id}/vote")
def news_vote_cast(news_id: str, req: NewsVoteIn, request: Request):
    voter = _voter_id(request, req.voter_id)
    ts = _now_ts()
    conn = get_stocks_conn()
    try:
        conn.execute(
            """
            INSERT INTO news_votes(news_id, voter_id, vote, created_at)
            VALUES(?,?,?,?)
            ON CONFLICT(news_id, voter_id) DO UPDATE SET
              vote=excluded.vote,
              created_at=excluded.created_at
            """,
            (news_id, voter, req.vote, ts),
        )
        conn.commit()
    finally:
        conn.close()
    return news_vote_summary(news_id, request)
