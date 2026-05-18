"""
건의사항(유저 피드백) 관련 API 라우터 (텔레그램 알림 버전)
"""
import logging
import requests
from typing import Optional

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel

from ..core.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from ..state import save_suggestion

logger = logging.getLogger("server.routes.feedback")

router = APIRouter()

class FeedbackRequest(BaseModel):
    # 신규: 프론트에서 보내는 닉네임. 구버전 호환을 위해 user_email 도 그대로 유지.
    nickname: Optional[str] = None
    user_email: Optional[str] = None
    content: str


def _resolve_sender(req: "FeedbackRequest") -> str:
    """nickname 우선, 없으면 user_email, 둘 다 없으면 '익명'."""
    for value in (req.nickname, req.user_email):
        if value and str(value).strip():
            return str(value).strip()
    return "익명"


def send_telegram_notification(sender: str, content: str):
    """텔레그램 봇을 사용하여 관리자에게 메시지 발송"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("[Feedback] TELEGRAM_BOT_TOKEN 또는 CHAT_ID 설정이 없어 알림을 보내지 못했습니다. DB에는 저장되었습니다.")
        return

    text = (
        "🔔 [JURINMAP 새 건의사항]\n\n"
        f"👤 닉네임: {sender}\n"
        f"📝 내용: {content}"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }

    try:
        res = requests.post(url, json=payload, timeout=10)
        res.raise_for_status()
        logger.info(f"[Feedback] 텔레그램 메시지를 성공적으로 발송했습니다.")
    except Exception as e:
        logger.error(f"[Feedback] 텔레그램 발송 중 오류 발생: {e}")

@router.post("/api/feedback")
def submit_feedback(req: FeedbackRequest, background_tasks: BackgroundTasks):
    if not req.content.strip():
        raise HTTPException(400, "내용이 비어있습니다.")

    sender = _resolve_sender(req)

    try:
        # 1. DB 저장 (기존 save_suggestion 시그니처 유지 — 식별자만 바뀜)
        save_suggestion(sender, req.content)

        # 2. 텔레그램 알림 발송 (백그라운드)
        background_tasks.add_task(send_telegram_notification, sender, req.content)

        return {"ok": True, "message": "건의사항이 소중히 접수되었습니다. 감사합니다!"}
    except Exception as e:
        logger.exception(f"[Feedback] 처리 오류: {e}")
        raise HTTPException(500, f"서버 처리 오류가 발생했습니다: {e}")
