"""
Push notification routes.
"""

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException

from ..cache import redis_claim_once
from ..state import (
    FIREBASE_VAPID_KEY,
    VAPID_PUBLIC_KEY,
    FIREBASE_WEB_CONFIG,
    TELEMSG_PUSH_TOKEN,
    _get_firebase_app,
    firebase_messaging,
    _save_fcm_token,
    _remove_fcm_token,
    _collect_fcm_tokens,
    _collect_fcm_token_rows,
    _upsert_push_subscription,
    _remove_push_subscription,
    _collect_push_subscriptions,
    _send_fcm_to_all,
    _send_webpush_to_all,
    _verify_push_dev_token,
    _record_pwa_install,
    _count_pwa_installs,
    set_price_alert,
    cancel_price_alert,
)

router = APIRouter()
logger = logging.getLogger("server.routes.push")


def _env_int(name: str, default: int, minimum: int) -> int:
    raw = str(os.getenv(name, str(default)) or str(default)).strip()
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(minimum, value)


_PUSH_NEWS_DEDUPE_TTL_SEC = _env_int("PUSH_NEWS_DEDUPE_TTL_SEC", 180, 10)
_PUSH_NEWS_DEDUPE_MAX_KEYS = _env_int("PUSH_NEWS_DEDUPE_MAX_KEYS", 5000, 100)
_PUSH_NEWS_DEDUPE_MEM: dict[str, float] = {}
_PUSH_NEWS_DEDUPE_LOCK = threading.Lock()


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def _first_line(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        for line in str(value).splitlines():
            text = _normalize_text(line)
            if text:
                return text
    return ""


def _coerce_meta(payload: dict) -> dict:
    raw_meta = (payload or {}).get("meta")
    if isinstance(raw_meta, dict):
        return raw_meta
    if isinstance(raw_meta, str):
        raw_meta = raw_meta.strip()
        if raw_meta:
            try:
                parsed = json.loads(raw_meta)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
    return {}


def _build_news_push_payload(payload: dict) -> dict:
    source = payload or {}
    meta = _coerce_meta(source)

    title = _normalize_text(
        source.get("title")
        or source.get("headline")
        or meta.get("title")
        or meta.get("headline")
        or _first_line(
            source.get("body"),
            source.get("message"),
            source.get("content"),
            source.get("raw_content"),
            meta.get("body"),
            meta.get("summary"),
            meta.get("content"),
            meta.get("raw_content"),
        )
    )

    body = _normalize_text(
        source.get("body")
        or source.get("message")
        or source.get("content")
        or source.get("raw_content")
        or meta.get("body")
        or meta.get("summary")
        or meta.get("content")
        or meta.get("raw_content")
        or title
    )

    if not title and body:
        title = _first_line(body)
    if not body and title:
        body = title
    if not title and not body:
        raise HTTPException(400, "title/body/content is required")

    # 뉴스 푸시 딥링크 — news_id 있으면 상세 페이지(/news/{id}) 직접, 없으면 목록(/news)
    explicit_url = _normalize_text(source.get("url") or meta.get("url"))
    if explicit_url:
        url = explicit_url
    else:
        news_id = _extract_news_dedupe_id(source, meta)
        url = f"/news/{news_id}" if news_id else "/news"
    tag = _normalize_text(source.get("tag") or meta.get("tag") or "news-alert") or "news-alert"
    icon = _normalize_text(source.get("icon") or meta.get("icon") or "/favicon.svg") or "/favicon.svg"

    # 알림 종류 (사용자 시간 설정 매트릭스에서 사용). 알 수 없으면 'news' 기본.
    raw_kind = _normalize_text(source.get("kind") or meta.get("kind") or "news")
    kind = raw_kind.lower() if raw_kind in ("news", "briefing", "stock", "NEWS", "BRIEFING", "STOCK") else "news"

    return {
        "title": title[:120],
        "body": body[:240],
        "url": url,
        "tag": tag,
        "icon": icon,
        "kind": kind,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "meta": meta,
    }


def _extract_news_dedupe_id(source: dict, meta: dict) -> str:
    candidates = [
        source.get("event_id"),
        source.get("news_id"),
        source.get("id"),
        source.get("message_id"),
        meta.get("event_id"),
        meta.get("news_id"),
        meta.get("id"),
        meta.get("message_id"),
    ]
    for value in candidates:
        text = _normalize_text(value)
        if text:
            return text[:200]
    return ""


def _build_news_dedupe_key(source: dict, push_payload: dict) -> str:
    src = source or {}
    meta = _coerce_meta(src)
    dedupe_id = _extract_news_dedupe_id(src, meta)

    if dedupe_id:
        basis = f"id|{dedupe_id}"
    else:
        raw_content = _normalize_text(
            src.get("raw_content")
            or src.get("content")
            or src.get("message")
            or meta.get("raw_content")
            or meta.get("content")
            or meta.get("summary")
        )
        headline = _normalize_text(
            src.get("headline")
            or src.get("title")
            or meta.get("headline")
            or meta.get("title")
            or push_payload.get("title")
        )
        body = _normalize_text(
            src.get("body")
            or src.get("message")
            or src.get("content")
            or meta.get("body")
            or meta.get("summary")
            or push_payload.get("body")
        )
        compact_text = raw_content[:400] if raw_content else ""
        if not compact_text:
            compact_text = "|".join(
                part
                for part in (
                    headline[:180],
                    body[:220],
                    _normalize_text(push_payload.get("url"))[:120],
                )
                if part
            )
        basis = f"text|{compact_text or 'empty'}"

    digest = hashlib.sha1(basis.encode("utf-8", errors="ignore")).hexdigest()
    return digest


def _prune_news_dedupe_cache_locked(now_ts: float):
    expired = [k for k, exp in _PUSH_NEWS_DEDUPE_MEM.items() if exp <= now_ts]
    for key in expired:
        _PUSH_NEWS_DEDUPE_MEM.pop(key, None)

    overflow = len(_PUSH_NEWS_DEDUPE_MEM) - _PUSH_NEWS_DEDUPE_MAX_KEYS
    if overflow > 0:
        oldest = sorted(_PUSH_NEWS_DEDUPE_MEM.items(), key=lambda item: item[1])[:overflow]
        for key, _ in oldest:
            _PUSH_NEWS_DEDUPE_MEM.pop(key, None)


def _reserve_news_dedupe(source: dict, push_payload: dict) -> tuple[bool, str]:
    dedupe_key = _build_news_dedupe_key(source, push_payload)
    redis_key = f"push:news:dedupe:{dedupe_key}"
    redis_claim = redis_claim_once(
        redis_key,
        {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "title": str(push_payload.get("title") or "")[:80]},
        ttl_seconds=_PUSH_NEWS_DEDUPE_TTL_SEC,
    )
    if redis_claim is True:
        return True, dedupe_key
    if redis_claim is False:
        return False, dedupe_key

    now_ts = time.time()
    with _PUSH_NEWS_DEDUPE_LOCK:
        _prune_news_dedupe_cache_locked(now_ts)
        exp = float(_PUSH_NEWS_DEDUPE_MEM.get(dedupe_key) or 0.0)
        if exp > now_ts:
            return False, dedupe_key
        _PUSH_NEWS_DEDUPE_MEM[dedupe_key] = now_ts + float(_PUSH_NEWS_DEDUPE_TTL_SEC)
        return True, dedupe_key


@router.get("/api/push/vapid-public-key")
def get_vapid_public_key():
    # Backward-compat endpoint: return Firebase VAPID key first.
    key = FIREBASE_VAPID_KEY or VAPID_PUBLIC_KEY
    if not key:
        raise HTTPException(503, "VAPID key is not configured")
    return {"publicKey": key}


@router.get("/api/push/firebase-config")
def get_firebase_config():
    config = {k: v for k, v in FIREBASE_WEB_CONFIG.items() if v}
    if not config.get("apiKey") or not config.get("projectId") or not config.get("messagingSenderId") or not config.get("appId"):
        raise HTTPException(503, "Firebase web config is not configured")
    config["vapidKey"] = FIREBASE_VAPID_KEY
    return config


@router.post("/api/push/subscribe")
def subscribe_push(payload: dict, user_agent: Optional[str] = Header(default=None)):
    try:
        token = str((payload or {}).get("token") or "").strip()
        if token:
            _save_fcm_token(
                token=token,
                user_agent=user_agent or "",
                platform=str((payload or {}).get("platform") or "web"),
            )
            return {"ok": True, "provider": "firebase"}

        # Backward compatibility: old WebPush subscription shape.
        _upsert_push_subscription(payload, user_agent=user_agent)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"subscription_failed: {e}")


@router.post("/api/push/unsubscribe")
def unsubscribe_push(payload: dict):
    token = str((payload or {}).get("token") or "").strip()
    endpoint = str((payload or {}).get("endpoint") or "").strip()
    if token:
        _remove_fcm_token(token)
        return {"ok": True, "provider": "firebase"}
    if endpoint:
        _remove_push_subscription(endpoint)
        return {"ok": True, "provider": "webpush"}
    raise HTTPException(400, "token or endpoint is required")


@router.post("/api/push/news")
def push_news_alert(payload: dict, x_telemsg_token: Optional[str] = Header(default=None)):
    if TELEMSG_PUSH_TOKEN and x_telemsg_token != TELEMSG_PUSH_TOKEN:
        raise HTTPException(403, "forbidden")

    push_payload = _build_news_push_payload(payload)
    should_send, dedupe_key = _reserve_news_dedupe(payload, push_payload)
    if not should_send:
        logger.info(
            "push/news deduped key=%s title=%s",
            dedupe_key[:12],
            str(push_payload.get("title") or "")[:80],
        )
        return {
            "ok": True,
            "provider": "dedupe",
            "deduped": True,
            "result": {"ok": True, "sent": 0, "failed": 0, "reason": "duplicate_suppressed"},
        }

    firebase_result = _send_fcm_to_all(push_payload)
    logger.info(
        "push/news firebase result: ok=%s sent=%s failed=%s reason=%s title=%s",
        firebase_result.get("ok"), firebase_result.get("sent"), firebase_result.get("failed"),
        firebase_result.get("reason"), str(push_payload.get("title") or "")[:60],
    )
    if firebase_result.get("ok"):
        return {"ok": True, "provider": "firebase", "result": firebase_result}

    # Fallback to generic webpush if Firebase is unavailable.
    webpush_result = _send_webpush_to_all(push_payload)
    logger.info("push/news webpush fallback: ok=%s", webpush_result.get("ok"))
    return {
        "ok": True,
        "provider": "webpush" if webpush_result.get("ok") else "none",
        "result": webpush_result,
        "firebase_result": firebase_result,
    }


@router.get("/api/push/preferences")
def get_notification_preferences(token: str):
    """토큰별 알림 종류×시간 환경설정 조회. 없으면 default."""
    if not token or len(token) < 10:
        raise HTTPException(400, "token required")
    try:
        from server.services.notification_pref import get_pref, DEFAULT_PREF, KNOWN_KINDS
    except Exception as e:
        raise HTTPException(503, f"pref_module_error: {e}")
    pref = get_pref(token)
    return {
        "ok": True,
        "token_prefix": token[:10],
        "pref": pref,
        "kinds_available": sorted(KNOWN_KINDS),
        "default": DEFAULT_PREF,
    }


@router.post("/api/push/preferences")
def set_notification_preferences(payload: dict):
    """토큰별 알림 환경설정 저장. body: {token, pref: {kinds:{...}}}"""
    token = (payload or {}).get("token") or ""
    raw_pref = (payload or {}).get("pref") or {}
    if not token or len(token) < 10:
        raise HTTPException(400, "token required")
    try:
        from server.services.notification_pref import upsert_pref
    except Exception as e:
        raise HTTPException(503, f"pref_module_error: {e}")
    try:
        normalized = upsert_pref(token, raw_pref)
    except Exception as e:
        logger.exception("notification_pref upsert failed")
        raise HTTPException(500, f"upsert_failed: {e}")
    return {"ok": True, "pref": normalized}


@router.get("/api/push/status")
def push_status():
    try:
        fcm_count = len(_collect_fcm_tokens())
    except Exception:
        fcm_count = 0
    try:
        webpush_count = len(_collect_push_subscriptions())
    except Exception:
        webpush_count = 0
    try:
        pwa_count = _count_pwa_installs()
    except Exception:
        pwa_count = 0

    return {
        "ok": True,
        "firebase_ready": bool(_get_firebase_app() and firebase_messaging),
        "fcm_subscribers": fcm_count,
        "webpush_subscribers": webpush_count,
        "pwa_installations": pwa_count,
    }


@router.post("/api/push/pwa/install")
def record_pwa_install_endpoint(payload: dict):
    install_id = str((payload or {}).get("install_id") or "").strip()
    user_agent = str((payload or {}).get("user_agent") or "").strip()
    platform = str((payload or {}).get("platform") or "").strip()
    if not install_id:
        raise HTTPException(400, "install_id is required")

    try:
        _record_pwa_install(install_id, user_agent=user_agent, platform=platform)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"record_failed: {e}")


@router.get("/api/push/dev/tokens")
def push_dev_tokens(limit: int = 50, q: str = "", x_push_dev_token: Optional[str] = Header(default=None)):
    _verify_push_dev_token(x_push_dev_token)
    rows = _collect_fcm_token_rows(limit=limit, q=q)
    return {
        "ok": True,
        "count": len(rows),
        "rows": rows,
    }


@router.post("/api/push/price-alert/set")
def set_price_alert_endpoint(payload: dict):
    token = str((payload or {}).get("token") or "").strip()
    code = str((payload or {}).get("code") or "").strip()
    direction = str((payload or {}).get("direction") or "above").strip()
    try:
        target_price = float((payload or {}).get("target_price") or 0)
    except (TypeError, ValueError):
        target_price = 0.0
    if not token or not code or target_price <= 0:
        raise HTTPException(400, "token, code, target_price(>0) required")
    if direction not in ("above", "below"):
        raise HTTPException(400, "direction must be 'above' or 'below'")
    set_price_alert(token, code, target_price, direction)
    return {"ok": True}


@router.delete("/api/push/price-alert/cancel")
def cancel_price_alert_endpoint(payload: dict):
    token = str((payload or {}).get("token") or "").strip()
    code = str((payload or {}).get("code") or "").strip()
    if not token or not code:
        raise HTTPException(400, "token and code required")
    cancel_price_alert(token, code)
    return {"ok": True}


@router.post("/api/push/dev/delete-token")
def push_dev_delete_token(payload: dict, x_push_dev_token: Optional[str] = Header(default=None)):
    _verify_push_dev_token(x_push_dev_token)
    token = str((payload or {}).get("token") or "").strip()
    if not token:
        raise HTTPException(400, "token is required")
    _remove_fcm_token(token)
    return {"ok": True}
