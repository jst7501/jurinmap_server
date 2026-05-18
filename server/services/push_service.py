"""Push delivery and token store service."""

import json
import logging
import os
from datetime import datetime
from typing import Optional

try:
    import firebase_admin
    from firebase_admin import credentials as firebase_credentials
    from firebase_admin import messaging as firebase_messaging

    # Firebase Admin SDK는 내부적으로 google.auth.transport.requests.AuthorizedSession
    # (= requests.Session 서브클래스)을 사용하고, send_each_for_multicast는
    # ThreadPoolExecutor로 토큰마다 병렬 HTTPS 요청을 날림. urllib3 기본
    # pool_maxsize=10 이라 200+ 구독자에겐 "Connection pool is full" 경고가
    # 대량 발생하고 매번 새 TCP/TLS handshake 발생 → 로그 스팸 + 성능 저하.
    #
    # requests.adapters.DEFAULT_POOLSIZE 를 사후 변경해도 HTTPAdapter.__init__
    # 의 default 인자는 def 시점에 bound 되어 있어 반영되지 않음.
    # → AuthorizedSession 생성 시 직접 adapter 를 mount 하도록 monkey-patch.
    try:
        from google.auth.transport.requests import AuthorizedSession as _AS
        from requests.adapters import HTTPAdapter as _HTTPAdapter
        _FCM_POOL_SIZE = max(50, int(os.getenv("FCM_HTTP_POOL_SIZE", "100")))
        if not getattr(_AS, "_jurin_pool_patched", False):
            _orig_as_init = _AS.__init__

            def _patched_as_init(self, *args, **kwargs):
                _orig_as_init(self, *args, **kwargs)
                try:
                    adapter = _HTTPAdapter(
                        pool_connections=_FCM_POOL_SIZE,
                        pool_maxsize=_FCM_POOL_SIZE,
                    )
                    self.mount("https://", adapter)
                    self.mount("http://", adapter)
                except Exception:
                    pass

            _AS.__init__ = _patched_as_init
            _AS._jurin_pool_patched = True
    except Exception:
        # google-auth 가 없거나 구조 바뀌면 조용히 패스 — FCM 자체는 계속 동작
        pass
except Exception:
    firebase_admin = None
    firebase_credentials = None
    firebase_messaging = None

try:
    from pywebpush import WebPushException, webpush
except Exception:
    webpush = None
    WebPushException = Exception

from ..core.settings import (
    FIREBASE_SERVICE_ACCOUNT_KEY,
    PUSH_DEV_ADMIN_TOKEN,
    VAPID_CLAIMS_SUB,
    VAPID_PRIVATE_KEY,
    VAPID_PUBLIC_KEY,
)
from ..db.connections import get_news_conn

logger = logging.getLogger("server.services.push_service")

_FIREBASE_APP = None
_PUSH_SCHEMA_READY = False
_FCM_SCHEMA_READY = False


def ensure_push_schema(conn):
    global _PUSH_SCHEMA_READY
    if _PUSH_SCHEMA_READY:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            endpoint      TEXT PRIMARY KEY,
            p256dh        TEXT NOT NULL,
            auth          TEXT NOT NULL,
            expiration    TEXT,
            user_agent    TEXT,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pwa_installs (
            install_id    TEXT PRIMARY KEY,
            user_agent    TEXT,
            platform      TEXT,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        )
        """
    )
    conn.commit()
    _PUSH_SCHEMA_READY = True


def _validate_push_subscription(subscription: dict):
    endpoint = str(subscription.get("endpoint") or "").strip()
    keys = subscription.get("keys") or {}
    p256dh = str(keys.get("p256dh") or "").strip()
    auth = str(keys.get("auth") or "").strip()
    expiration = subscription.get("expirationTime")

    if not endpoint or not p256dh or not auth:
        raise ValueError("Invalid push subscription payload")

    return {
        "endpoint": endpoint,
        "p256dh": p256dh,
        "auth": auth,
        "expiration": str(expiration) if expiration is not None else None,
    }


def _upsert_push_subscription(subscription: dict, user_agent: Optional[str] = None):
    payload = _validate_push_subscription(subscription)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_news_conn()
    try:
        ensure_push_schema(conn)
        conn.execute(
            """
            INSERT INTO push_subscriptions(endpoint, p256dh, auth, expiration, user_agent, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                p256dh=excluded.p256dh,
                auth=excluded.auth,
                expiration=excluded.expiration,
                user_agent=excluded.user_agent,
                updated_at=excluded.updated_at
            """,
            (
                payload["endpoint"],
                payload["p256dh"],
                payload["auth"],
                payload["expiration"],
                (user_agent or "")[:300],
                ts,
                ts,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _remove_push_subscription(endpoint: str):
    if not endpoint:
        return
    conn = get_news_conn()
    try:
        ensure_push_schema(conn)
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
        conn.commit()
    finally:
        conn.close()


def _collect_push_subscriptions():
    conn = get_news_conn()
    try:
        ensure_push_schema(conn)
        rows = conn.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _webpush_config_ready():
    return bool(webpush and VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)


def _send_webpush_to_all(payload: dict):
    if not _webpush_config_ready():
        return {
            "ok": False,
            "sent": 0,
            "failed": 0,
            "reason": "webpush_not_configured",
        }

    subscriptions = _collect_push_subscriptions()
    if not subscriptions:
        return {"ok": True, "sent": 0, "failed": 0, "reason": "no_subscribers"}

    serialized_payload = json.dumps(payload, ensure_ascii=False)
    sent = 0
    failed = 0

    for row in subscriptions:
        sub = {
            "endpoint": row["endpoint"],
            "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
        }
        try:
            webpush(
                subscription_info=sub,
                data=serialized_payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_CLAIMS_SUB},
            )
            sent += 1
        except WebPushException as e:
            failed += 1
            if "410" in str(e):
                _remove_push_subscription(row["endpoint"])

    return {"ok": failed == 0, "sent": sent, "failed": failed, "reason": None}


def _get_token_store():
    """Token store: Postgres (via get_news_conn)."""
    return get_news_conn()


def _ensure_fcm_schema(conn):
    global _FCM_SCHEMA_READY
    if _FCM_SCHEMA_READY:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fcm_tokens (
            token       TEXT PRIMARY KEY,
            user_agent  TEXT,
            platform    TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
        """
    )
    conn.commit()
    _FCM_SCHEMA_READY = True


def _save_fcm_token(token: str, user_agent: str = "", platform: str = ""):
    token = str(token or "").strip()
    if not token:
        raise ValueError("token is required")

    conn = _get_token_store()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        _ensure_fcm_schema(conn)
        conn.execute(
            """
            INSERT INTO fcm_tokens(token, user_agent, platform, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(token) DO UPDATE SET
                user_agent=excluded.user_agent,
                platform=excluded.platform,
                updated_at=excluded.updated_at
            """,
            (token, (user_agent or "")[:300], (platform or "web")[:32], ts, ts),
        )
        conn.commit()
    finally:
        conn.close()


def _remove_fcm_token(token: str):
    token = str(token or "").strip()
    if not token:
        return

    conn = _get_token_store()
    try:
        _ensure_fcm_schema(conn)
        conn.execute("DELETE FROM fcm_tokens WHERE token=?", (token,))
        conn.commit()
    finally:
        conn.close()


def _collect_fcm_tokens():
    conn = _get_token_store()
    try:
        _ensure_fcm_schema(conn)
        rows = conn.execute("SELECT token FROM fcm_tokens").fetchall()
        return [str(r["token"] or "") for r in rows if r["token"]]
    finally:
        conn.close()


def _collect_fcm_token_rows(limit: int = 50, q: str = ""):
    limit = max(1, min(int(limit or 50), 500))
    q = str(q or "").strip()

    conn = _get_token_store()
    try:
        _ensure_fcm_schema(conn)
        if q:
            rows = conn.execute(
                """
                SELECT token, platform, user_agent, created_at, updated_at
                FROM fcm_tokens
                WHERE token LIKE ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (f"%{q}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT token, platform, user_agent, created_at, updated_at
                FROM fcm_tokens
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        out = []
        for r in rows:
            out.append(
                {
                    "token": str(r["token"] or ""),
                    "platform": str(r["platform"] or ""),
                    "user_agent": str(r["user_agent"] or ""),
                    "created_at": str(r["created_at"] or ""),
                    "updated_at": str(r["updated_at"] or ""),
                }
            )
        return out
    finally:
        conn.close()


def _verify_push_dev_token(x_push_dev_token: Optional[str]):
    from fastapi import HTTPException

    if PUSH_DEV_ADMIN_TOKEN and str(x_push_dev_token or "").strip() != PUSH_DEV_ADMIN_TOKEN:
        raise HTTPException(403, "forbidden")


def _get_firebase_app():
    global _FIREBASE_APP
    if _FIREBASE_APP is not None:
        return _FIREBASE_APP

    if not firebase_admin or not firebase_credentials or not firebase_messaging:
        return None
    if not FIREBASE_SERVICE_ACCOUNT_KEY:
        return None

    try:
        service_account_json = FIREBASE_SERVICE_ACCOUNT_KEY
        if service_account_json.startswith("'") and service_account_json.endswith("'"):
            service_account_json = service_account_json[1:-1]
        info = json.loads(service_account_json)
        if isinstance(info.get("private_key"), str):
            info["private_key"] = info["private_key"].replace("\\n", "\n")
        cred = firebase_credentials.Certificate(info)
        _FIREBASE_APP = firebase_admin.initialize_app(cred)
    except Exception:
        _FIREBASE_APP = None
    return _FIREBASE_APP


def _with_proxy_disabled(fn):
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    previous = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        return fn()
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _send_fcm_to_token(token: str, payload: dict):
    app = _get_firebase_app()
    if app is None or firebase_messaging is None:
        return
    title = str(payload.get("title") or "Alert")
    body = str(payload.get("body") or "")
    # 2026-04-24: HashRouter 잔해(/#/news) 제거 — 현 라우터는 BrowserRouter /news
    url = str(payload.get("url") or "/news")
    tag = str(payload.get("tag") or "price-alert")
    icon = str(payload.get("icon") or "/icons/icon-192.png")
    data = {}
    for k, v in payload.items():
        if v is None:
            continue
        if isinstance(v, (dict, list)):
            data[k] = json.dumps(v, ensure_ascii=False)
        else:
            data[k] = str(v)
    # title/body/icon/tag가 payload에 없을 수 있으므로 data에도 기본값 주입.
    data.setdefault("title", title)
    data.setdefault("body", body)
    data.setdefault("icon", icon)
    data.setdefault("tag", tag)
    data.setdefault("url", url)
    use_fcm_link = url.startswith("https://")
    # data-only 메시지 — Firebase SDK 자동 표시 경로 차단 (SW 리스너가 단독 처리)
    webpush_kwargs = {}
    if use_fcm_link:
        webpush_kwargs["fcm_options"] = firebase_messaging.WebpushFCMOptions(link=url)
    message = firebase_messaging.Message(
        token=token,
        data=data,
        webpush=firebase_messaging.WebpushConfig(**webpush_kwargs),
    )
    try:
        _with_proxy_disabled(lambda: firebase_messaging.send(message, app=app))
    except Exception as e:
        err = str(e).lower()
        if any(k in err for k in ("not registered", "invalid registration token", "sender id")):
            _remove_fcm_token(token)
        raise


def _send_fcm_to_all(payload: dict):
    app = _get_firebase_app()
    if app is None or firebase_messaging is None:
        return {"ok": False, "sent": 0, "failed": 0, "reason": "firebase_not_configured"}

    tokens = _collect_fcm_tokens()
    if not tokens:
        return {"ok": True, "sent": 0, "failed": 0, "reason": "no_subscribers"}

    # 사용자별 알림 종류×시간 환경설정 필터링 (pref 없으면 모두 통과 = backward compat)
    kind = str(payload.get("kind") or "news").lower()
    try:
        from server.services.notification_pref import filter_allowed_tokens
        before_count = len(tokens)
        tokens = filter_allowed_tokens(tokens, kind)
        if before_count != len(tokens):
            logger.info(
                "notification_pref filtered: kind=%s before=%d after=%d",
                kind, before_count, len(tokens),
            )
        if not tokens:
            return {"ok": True, "sent": 0, "failed": 0, "reason": "no_subscribers_after_pref_filter"}
    except Exception:
        logger.exception("notification_pref filter error — skipping filter")

    sent = 0
    failed = 0
    invalid_tokens = []
    title = str(payload.get("title") or "News Alert")
    body = str(payload.get("body") or "There is a new update.")
    # 2026-04-24: HashRouter 잔해(/#/news) 제거 — 현 라우터는 BrowserRouter /news
    url = str(payload.get("url") or "/news")
    tag = str(payload.get("tag") or "news-alert")
    icon = str(payload.get("icon") or "/favicon.svg")

    data = {
        "title": title,
        "body": body,
        "url": url,
        "tag": tag,
        "icon": icon,
        "timestamp": str(payload.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    }
    if payload.get("headline") is not None:
        data["headline"] = str(payload.get("headline"))
    if payload.get("content") is not None:
        data["content"] = str(payload.get("content"))
    if payload.get("raw_content") is not None:
        data["raw_content"] = str(payload.get("raw_content"))
    if payload.get("meta") is not None:
        if isinstance(payload.get("meta"), (dict, list)):
            data["meta"] = json.dumps(payload.get("meta"), ensure_ascii=False)
        else:
            data["meta"] = str(payload.get("meta"))
    use_fcm_link = isinstance(url, str) and url.startswith("https://")

    batch_size = 500
    for i in range(0, len(tokens), batch_size):
        batch = tokens[i : i + batch_size]
        # NOTE: webpush.notification을 의도적으로 넣지 않음 → data-only 메시지.
        # Firebase SDK가 브라우저에서 자동으로 알림을 표시하지 않고, 우리 SW의
        # push 리스너가 단일 경로로 처리 → 중복 알림 차단.
        # fcm_options.link는 click-to-open URL만 지정 (SW에서 필요 시 사용).
        webpush_kwargs = {}
        if use_fcm_link:
            webpush_kwargs["fcm_options"] = firebase_messaging.WebpushFCMOptions(link=url)

        message = firebase_messaging.MulticastMessage(
            tokens=batch,
            data={k: str(v) for k, v in data.items()},
            webpush=firebase_messaging.WebpushConfig(**webpush_kwargs),
        )
        try:
            response = _with_proxy_disabled(
                lambda: firebase_messaging.send_each_for_multicast(message, app=app)
            )
        except Exception as e:
            return {
                "ok": False,
                "sent": sent,
                "failed": failed + len(batch),
                "reason": f"firebase_send_error: {type(e).__name__}: {e}",
            }
        sent += response.success_count
        failed += response.failure_count

        for idx, r in enumerate(response.responses):
            if r.success:
                continue
            err_code = getattr(getattr(r, "exception", None), "code", "")
            err_text = str(getattr(r, "exception", "")).lower()
            if (
                err_code in ("registration-token-not-registered", "invalid-argument", "mismatched-credential")
                or "not registered" in err_text
                or "invalid registration token" in err_text
                or "sender id" in err_text
            ):
                invalid_tokens.append(batch[idx])

    for token in invalid_tokens:
        _remove_fcm_token(token)

    return {"ok": True, "sent": sent, "failed": failed, "reason": None}


def _record_pwa_install(install_id: str, user_agent: str = "", platform: str = ""):
    install_id = str(install_id or "").strip()
    if not install_id:
        raise ValueError("install_id is required")

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_news_conn()
    try:
        ensure_push_schema(conn)
        conn.execute(
            """
            INSERT INTO pwa_installs(install_id, user_agent, platform, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(install_id) DO UPDATE SET
                user_agent=excluded.user_agent,
                platform=excluded.platform,
                updated_at=excluded.updated_at
            """,
            (install_id, user_agent, platform, ts, ts),
        )
        conn.commit()
    finally:
        conn.close()


def _count_pwa_installs() -> int:
    conn = get_news_conn()
    try:
        ensure_push_schema(conn)
        row = conn.execute("SELECT COUNT(*) FROM pwa_installs").fetchone()
        return row[0] if row else 0
    except Exception:
        return 0
    finally:
        conn.close()


__all__ = [
    "firebase_messaging",
    "ensure_push_schema",
    "_upsert_push_subscription",
    "_remove_push_subscription",
    "_collect_push_subscriptions",
    "_webpush_config_ready",
    "_send_webpush_to_all",
    "_save_fcm_token",
    "_remove_fcm_token",
    "_collect_fcm_tokens",
    "_collect_fcm_token_rows",
    "_verify_push_dev_token",
    "_get_firebase_app",
    "_send_fcm_to_token",
    "_send_fcm_to_all",
    "_record_pwa_install",
    "_count_pwa_installs",
]
