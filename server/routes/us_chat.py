"""US 페니 단타 라이브 채팅 — 익명 닉네임 + 폴링 기반.

스키마: us_chat_messages
  id BIGSERIAL PK
  client_id TEXT (브라우저 localStorage UUID — rate limit + 동일 사용자 식별)
  nickname TEXT (자동 생성, 변경 가능)
  message TEXT
  symbol TEXT NULL (선택 — 채팅 안에 종목 ticker mention)
  created_at TIMESTAMP

slow mode: 채팅방 전역 메시지 속도(최근 60초 DB 카운트)로 과열을 감지 →
           평온/주의/과열 3단계, 단계별로 client 당 전송 간격을 1/5/12초로
           동적 조절 (운영자 면제). 초과 전송은 429 + 슬로우 모드 정보 반환.
moderation: 메시지 1000자 제한, URL/이모지 자유.

Polling: GET /api/overseas/chat/messages?since=<timestamp_ms>&limit=50
        → 최근 N건 (since 이후만)
Send:    POST /api/overseas/chat/send {client_id, nickname, message, symbol?}
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()
logger = logging.getLogger("server.routes.us_chat")

MAX_MESSAGE_LEN = 1000
MAX_NICKNAME_LEN = 20
ADMIN_SESSION_DAYS = 365  # 운영자 admin_token 유효 기간
ADMIN_DISPLAY_NICK = "운영자"  # DB·응답에 저장될 운영자 표시 닉네임

# ─── 슬로우 모드 (과열 방지) ──────────────────────────────────────────────
# 채팅방 전역 메시지 속도(최근 60초 DB 카운트)로 과열을 감지해 모두에게
# 전송 간격을 동적으로 늘린다. 운영자(is_admin)는 면제. 속도는 DB 에서
# 직접 세므로 멀티 워커에서도 정확.
#   calm  평온 — 60초 < 40건 → client 당 1초 간격
#   warn  주의 — 60초 40건+  → client 당 5초 간격
#   hot   과열 — 60초 80건+  → client 당 12초 간격
SLOW_WINDOW_SEC = 60       # 전역 속도 측정 창 (초)
TIER_WARN_RATE  = 40       # 창 안 이 건수 이상 → 주의
TIER_HOT_RATE   = 80       # 창 안 이 건수 이상 → 과열
COOLDOWN_CALM   = 1        # 평온 — client 당 최소 전송 간격 (초)
COOLDOWN_WARN   = 5        # 주의
COOLDOWN_HOT    = 12       # 과열
ADMIN_MIN_GAP   = 0.6      # 운영자 최소 전송 간격 (연타 사고 방지)

# 운영자 인증 초기 setup — env 로 한 번 등록 후 DB hash 저장.
#   ADMIN_INIT_NICK=<원본 닉네임 평문>     (예: 본인 식별 ID)
#   ADMIN_INIT_PASSWORD=<원본 비밀번호 평문>
# 한 번 init 후엔 .env 에서 제거해도 됨 (DB row 가 있으면 skip).
# 코드·응답 어디에도 원본 nick/pwd 평문 노출 X — 모두 sha256 hash 비교.


def _hash_text(text: str, salt: str = "") -> str:
    return hashlib.sha256((salt + text).encode("utf-8")).hexdigest()


def _const_eq(a: str, b: str) -> bool:
    if not a or not b:
        return False
    try:
        return hmac.compare_digest(a, b)
    except Exception:
        return False


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS us_chat_messages (
            id BIGSERIAL PRIMARY KEY,
            client_id TEXT,
            nickname TEXT,
            message TEXT NOT NULL,
            symbol TEXT,
            owned_symbols TEXT,
            is_admin BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP NOT NULL
        )
        """
    )
    # 기존 테이블 마이그레이션 — 컬럼 없으면 추가
    for col, ddl in [
        ("owned_symbols", "ALTER TABLE us_chat_messages ADD COLUMN IF NOT EXISTS owned_symbols TEXT"),
        ("is_admin", "ALTER TABLE us_chat_messages ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE"),
    ]:
        try:
            conn.execute(ddl)
        except Exception:
            pass
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_us_chat_created ON us_chat_messages(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_us_chat_client ON us_chat_messages(client_id, created_at DESC)")
        conn.commit()
    except Exception:
        pass


def _ensure_admin_tables(conn) -> None:
    """운영자 인증 정보 + 세션 토큰 테이블."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS us_chat_admin_creds (
            id INTEGER PRIMARY KEY,
            nick_hash TEXT NOT NULL,
            pwd_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS us_chat_admin_sessions (
            token_hash TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
        """
    )
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_us_chat_admin_sess_client ON us_chat_admin_sessions(client_id)")
        conn.commit()
    except Exception:
        pass


def _maybe_init_admin_creds(conn) -> None:
    """env ADMIN_INIT_NICK + ADMIN_INIT_PASSWORD 있고 DB row 없으면 hash 저장.
    init 후 env 변수 제거 권장 — 평문 nick/pwd 흔적 최소화.
    """
    init_nick = (os.environ.get("ADMIN_INIT_NICK") or "").strip()
    init_pwd = (os.environ.get("ADMIN_INIT_PASSWORD") or "").strip()
    if not init_nick or not init_pwd:
        return
    cur = conn.execute("SELECT 1 FROM us_chat_admin_creds WHERE id = 1")
    if cur.fetchone():
        return
    salt = secrets.token_hex(16)
    nick_h = _hash_text(init_nick.lower())
    pwd_h = _hash_text(init_pwd, salt)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    conn.execute(
        """
        INSERT INTO us_chat_admin_creds (id, nick_hash, pwd_hash, salt, created_at)
        VALUES (1, %s, %s, %s, %s)
        """,
        (nick_h, pwd_h, salt, now),
    )
    try:
        conn.commit()
    except Exception:
        pass
    logger.info("admin creds initialized in DB (id=1) from env")


def _get_admin_creds(conn) -> Optional[tuple[str, str, str]]:
    cur = conn.execute("SELECT nick_hash, pwd_hash, salt FROM us_chat_admin_creds WHERE id = 1")
    r = cur.fetchone()
    if not r:
        return None
    return (r[0], r[1], r[2])


def _is_admin_nick_input(conn, nickname: str) -> bool:
    """입력 nickname 이 운영자 닉네임에 해당하는지 (hash 비교 또는 display nick).
    coderbase 에 원본 식별 문자열 노출 X — DB nick_hash 와 비교만.
    """
    if not nickname:
        return False
    n = nickname.strip()
    if not n:
        return False
    # display nick (평문 '운영자') 도 보호 대상
    if n.lower() == ADMIN_DISPLAY_NICK.lower():
        return True
    creds = _get_admin_creds(conn)
    if not creds:
        return False
    input_h = _hash_text(n.lower())
    return _const_eq(creds[0], input_h)


def _verify_admin_password(conn, nickname: str, password: str) -> bool:
    if not nickname or not password:
        return False
    creds = _get_admin_creds(conn)
    if not creds:
        return False
    nick_h, pwd_h, salt = creds
    if not _const_eq(nick_h, _hash_text(nickname.strip().lower())):
        return False
    return _const_eq(pwd_h, _hash_text(password, salt))


def _issue_admin_session(conn, client_id: str) -> str:
    """랜덤 token 발급 후 DB 에 hash 만 저장. 반환은 평문 token (frontend localStorage 저장용)."""
    token = secrets.token_urlsafe(32)
    token_h = _hash_text(token)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    expires = now + timedelta(days=ADMIN_SESSION_DAYS)
    conn.execute(
        """
        INSERT INTO us_chat_admin_sessions (token_hash, client_id, expires_at, created_at)
        VALUES (%s, %s, %s, %s)
        """,
        (token_h, client_id, expires, now),
    )
    try:
        conn.commit()
    except Exception:
        pass
    return token


def _verify_admin_session(conn, token: Optional[str], client_id: Optional[str]) -> bool:
    if not token or not client_id:
        return False
    token_h = _hash_text(str(token).strip())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        cur = conn.execute(
            """
            SELECT 1 FROM us_chat_admin_sessions
            WHERE token_hash = %s AND client_id = %s AND expires_at > %s
            """,
            (token_h, client_id, now),
        )
        return cur.fetchone() is not None
    except Exception:
        return False


def _parse_owned(raw) -> str | None:
    """owned_symbols 입력 → JSON string 정규화.
    각 항목은 { ticker, mode, avg? } object 또는 단순 string(=ticker, holding).
    mode: "holding" (보유) | "watching" (관망)
    avg: 평단가 (mode=holding 일 때만 의미)
    """
    import json as _json
    if not raw:
        return None
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        try:
            items = _json.loads(raw)
            if not isinstance(items, list):
                return None
        except Exception:
            items = [s.strip() for s in raw.split(",") if s.strip()]
    else:
        return None

    cleaned = []
    for it in items[:5]:
        if isinstance(it, dict):
            t = str(it.get("ticker") or "").strip().upper()
            if not t or len(t) > 6:
                continue
            entry = {"ticker": t}
            mode = str(it.get("mode") or "holding").strip().lower()
            if mode not in ("holding", "watching"):
                mode = "holding"
            entry["mode"] = mode
            avg = it.get("avg")
            try:
                if avg is not None and avg != "":
                    avgf = float(avg)
                    if avgf > 0:
                        entry["avg"] = round(avgf, 4)
            except Exception:
                pass
            cleaned.append(entry)
        elif it:
            # legacy: 단순 ticker string
            t = str(it).strip().upper()
            if t and len(t) <= 6:
                cleaned.append({"ticker": t, "mode": "holding"})
    return _json.dumps(cleaned) if cleaned else None


def _unpack_owned(raw) -> list:
    """저장된 owned_symbols → list[dict] (legacy string 도 dict 화)."""
    if not raw:
        return []
    try:
        import json as _json
        v = _json.loads(raw)
        if not isinstance(v, list):
            return []
        out = []
        for it in v:
            if isinstance(it, dict):
                out.append(it)
            elif it:
                out.append({"ticker": str(it).strip().upper(), "mode": "holding"})
        return out
    except Exception:
        return []


def _slow_tier(rate: int) -> tuple[str, int]:
    """전역 메시지 rate → (tier, cooldown_sec)."""
    if rate >= TIER_HOT_RATE:
        return ("hot", COOLDOWN_HOT)
    if rate >= TIER_WARN_RATE:
        return ("warn", COOLDOWN_WARN)
    return ("calm", COOLDOWN_CALM)


def _global_rate(conn) -> int:
    """최근 SLOW_WINDOW_SEC 초 동안 채팅방 전체 메시지 수 (DB 카운트)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=SLOW_WINDOW_SEC)).replace(tzinfo=None)
    try:
        cur = conn.execute("SELECT COUNT(*) FROM us_chat_messages WHERE created_at > %s", (cutoff,))
        return int(cur.fetchone()[0] or 0)
    except Exception:
        return 0


def _client_last_send_age(conn, client_id: str) -> Optional[float]:
    """해당 client 의 마지막 메시지 후 경과 초. 기록 없으면 None."""
    try:
        cur = conn.execute(
            "SELECT created_at FROM us_chat_messages WHERE client_id = %s ORDER BY created_at DESC LIMIT 1",
            (client_id,),
        )
        r = cur.fetchone()
    except Exception:
        return None
    if not r or not r[0]:
        return None
    last = r[0]
    if hasattr(last, "replace"):
        last = last.replace(tzinfo=timezone.utc)
    try:
        return (datetime.now(timezone.utc) - last).total_seconds()
    except Exception:
        return None


def _slow_mode_state(conn) -> dict:
    """현재 슬로우 모드 상태 — /messages 응답에 포함 (프론트 인디케이터용)."""
    rate = _global_rate(conn)
    tier, cooldown = _slow_tier(rate)
    return {"tier": tier, "cooldown_sec": cooldown, "rate_per_min": rate}


def _check_send_allowed(conn, client_id: str, is_admin: bool) -> tuple[bool, float, dict]:
    """전송 가능 여부 판정.
    반환: (허용?, retry_after_sec, slow_mode dict)
      slow_mode 는 항상 '방 전체' 상태 (tier·cooldown·rate) — 개인 gap 과 무관.
    """
    rate = _global_rate(conn)
    tier, cooldown = _slow_tier(rate)
    sm = {"tier": tier, "cooldown_sec": cooldown, "rate_per_min": rate}
    gap = ADMIN_MIN_GAP if is_admin else float(cooldown)
    age = _client_last_send_age(conn, client_id)
    if age is not None and age < gap:
        return (False, round(gap - age, 1), sm)
    return (True, 0.0, sm)


@router.get("/api/overseas/chat/messages")
async def chat_messages(
    since: Optional[int] = Query(None, description="unix ms — 이후 메시지만 (실시간 폴링)"),
    before_id: Optional[int] = Query(None, description="bigint id — 이전 메시지만 (무한 스크롤)"),
    limit: int = Query(30, ge=1, le=100),
):
    """최근 N건 메시지.
    - since: 이후 메시지 (실시간 폴링용)
    - before_id: 이전 메시지 (무한 스크롤 — older 30건씩)
    - 둘 다 없으면 초기 진입 — 최근 limit 건.
    운영자 메시지는 nickname='운영자' + is_admin=TRUE 로 노출. 식별 원본 닉네임은 저장도·응답도 X.
    """
    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        _ensure_table(conn)
        if before_id:
            cur = conn.execute(
                f"""
                SELECT id, client_id, nickname, message, symbol, owned_symbols, is_admin, created_at FROM (
                    SELECT id, client_id, nickname, message, symbol, owned_symbols, is_admin, created_at
                    FROM us_chat_messages
                    WHERE id < %s
                    ORDER BY id DESC LIMIT {int(limit)}
                ) x ORDER BY created_at ASC
                """,
                (int(before_id),),
            )
        elif since:
            since_dt = datetime.fromtimestamp(since / 1000.0, tz=timezone.utc).replace(tzinfo=None)
            cur = conn.execute(
                """
                SELECT id, client_id, nickname, message, symbol, owned_symbols, is_admin, created_at
                FROM us_chat_messages
                WHERE created_at > %s
                ORDER BY created_at ASC LIMIT %s
                """,
                (since_dt, limit),
            )
        else:
            cur = conn.execute(
                f"""
                SELECT id, client_id, nickname, message, symbol, owned_symbols, is_admin, created_at FROM (
                    SELECT id, client_id, nickname, message, symbol, owned_symbols, is_admin, created_at
                    FROM us_chat_messages
                    ORDER BY created_at DESC LIMIT {int(limit)}
                ) x ORDER BY created_at ASC
                """
            )
        rows = cur.fetchall()
        slow_mode = _slow_mode_state(conn)
    finally:
        conn.close()

    return {
        "status": "ok",
        "has_more": len(rows) >= limit,
        "slow_mode": slow_mode,
        "messages": [
            {
                "id": int(r[0]),
                "client_id": r[1],
                "nickname": r[2] or "익명",
                "is_admin": bool(r[6]) if r[6] is not None else False,
                "message": r[3],
                "symbol": r[4],
                "owned_symbols": _unpack_owned(r[5]),
                "created_at": r[7].isoformat() if r[7] and hasattr(r[7], "isoformat") else r[7],
                "created_at_ms": int(r[7].replace(tzinfo=timezone.utc).timestamp() * 1000) if r[7] and hasattr(r[7], "replace") else None,
            }
            for r in rows
        ],
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


@router.post("/api/overseas/chat/verify_admin")
async def chat_verify_admin(payload: dict):
    """운영자 nickname + password 인증 → admin_token 발급.
    - 성공: { admin_token: '<long token>', display_nick: '운영자' }
    - 실패: 401 (원본 식별 문자열은 응답에 노출 X)
    body: { nickname, password, client_id }
    """
    nickname = str((payload or {}).get("nickname") or "").strip()
    password = str((payload or {}).get("password") or "")
    client_id = str((payload or {}).get("client_id") or "").strip()
    if not nickname or not password:
        raise HTTPException(400, "닉네임·비밀번호 필요")
    if not client_id or len(client_id) < 8:
        raise HTTPException(400, "client_id required (UUID)")

    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        _ensure_admin_tables(conn)
        _maybe_init_admin_creds(conn)
        if not _verify_admin_password(conn, nickname, password):
            raise HTTPException(401, "인증 실패 — 닉네임 또는 비밀번호가 달라요")
        token = _issue_admin_session(conn, client_id)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return {"status": "ok", "admin_token": token, "display_nick": ADMIN_DISPLAY_NICK}


@router.get("/api/overseas/chat/admin_status")
async def chat_admin_status(
    client_id: Optional[str] = Query(None),
    admin_token: Optional[str] = Query(None),
):
    """프론트가 페이지 진입 시 현재 admin_token 유효성 확인용. 만료 시 frontend 가 token 제거."""
    if not client_id or not admin_token:
        return {"status": "ok", "is_admin": False}
    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        _ensure_admin_tables(conn)
        ok = _verify_admin_session(conn, admin_token, client_id)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {"status": "ok", "is_admin": bool(ok), "display_nick": ADMIN_DISPLAY_NICK if ok else None}


@router.post("/api/overseas/chat/send")
async def chat_send(payload: dict):
    """메시지 전송. body: {client_id, nickname, message, symbol?, admin_token?}
    - admin nickname 입력 + admin_token 검증 통과 → nickname '운영자'로 자동 변환 + is_admin=True 저장
    - admin nickname 입력했는데 token 없거나 검증 실패 → 401 (그 닉네임 사용 불가)
    """
    client_id = str((payload or {}).get("client_id") or "").strip()
    nickname = str((payload or {}).get("nickname") or "익명").strip()[:MAX_NICKNAME_LEN]
    message = str((payload or {}).get("message") or "").strip()
    symbol = str((payload or {}).get("symbol") or "").strip().upper() or None
    owned_json = _parse_owned((payload or {}).get("owned_symbols"))
    admin_token = (payload or {}).get("admin_token")

    if not client_id or len(client_id) < 8:
        raise HTTPException(400, "client_id required (UUID)")
    if not message:
        raise HTTPException(400, "message required")
    if len(message) > MAX_MESSAGE_LEN:
        raise HTTPException(400, f"message too long (max {MAX_MESSAGE_LEN} chars)")

    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    is_admin = False
    save_nickname = nickname or "익명"
    try:
        _ensure_table(conn)
        _ensure_admin_tables(conn)
        _maybe_init_admin_creds(conn)

        admin_nick_attempted = _is_admin_nick_input(conn, nickname)
        if admin_nick_attempted:
            # admin 닉네임 시도 → admin_token 필수 + 유효
            if not _verify_admin_session(conn, admin_token, client_id):
                raise HTTPException(401, "이 닉네임은 사용 권한이 필요해요.")
            is_admin = True
            save_nickname = ADMIN_DISPLAY_NICK  # DB·응답에 원본 식별 닉 노출 X

        allowed, retry_after, slow_mode = _check_send_allowed(conn, client_id, is_admin)
        if not allowed:
            raise HTTPException(429, detail={
                "error": "slow_mode",
                "tier": slow_mode["tier"],
                "cooldown_sec": slow_mode["cooldown_sec"],
                "retry_after_sec": retry_after,
                "message": f"채팅이 빨라요 — {retry_after:.0f}초 후 다시 보낼 수 있어요",
            })

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        cur = conn.execute(
            """
            INSERT INTO us_chat_messages (client_id, nickname, message, symbol, owned_symbols, is_admin, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id, created_at
            """,
            (client_id, save_nickname, message, symbol, owned_json, is_admin, now),
        )
        r = cur.fetchone()
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return {
        "status": "ok",
        "id": int(r[0]),
        "nickname": save_nickname,
        "is_admin": is_admin,
        "message": message,
        "symbol": symbol,
        "owned_symbols": _unpack_owned(owned_json),
        "created_at": r[1].isoformat() if r[1] and hasattr(r[1], "isoformat") else None,
        "created_at_ms": int(r[1].replace(tzinfo=timezone.utc).timestamp() * 1000) if r[1] and hasattr(r[1], "replace") else None,
        "slow_mode": slow_mode,
        "next_cooldown_sec": 0 if is_admin else slow_mode["cooldown_sec"],
    }


@router.get("/api/overseas/chat/online")
async def chat_online_count():
    """최근 5분 안에 메시지 보낸 client_id 수 — 'N명 채팅중' 표시용."""
    from server.db.connections import get_stocks_conn
    from datetime import timedelta as _td
    cutoff = (datetime.now(timezone.utc) - _td(minutes=5)).replace(tzinfo=None)
    conn = get_stocks_conn()
    try:
        _ensure_table(conn)
        cur = conn.execute(
            "SELECT COUNT(DISTINCT client_id) FROM us_chat_messages WHERE created_at >= %s",
            (cutoff,),
        )
        n = int(cur.fetchone()[0] or 0)
    finally:
        conn.close()
    return {"status": "ok", "online_5m": n}
