import json
import logging
import os
import time
from typing import Any, Optional

try:
    import redis
except Exception:
    redis = None


logger = logging.getLogger("server.cache")

try:
    from .monitoring import observe_redis_get, observe_redis_set, observe_redis_claim
except Exception:
    def observe_redis_get(kind: str):
        return None
    def observe_redis_set(kind: str):
        return None
    def observe_redis_claim(kind: str):
        return None

_CLIENT = None
_LAST_FAIL_AT = 0.0
_FAIL_COOLDOWN_SEC = 10
_PREFIX = (os.getenv("REDIS_KEY_PREFIX", "investpulse") or "investpulse").strip()


def _redis_url() -> str:
    explicit = (os.getenv("REDIS_URL", "") or "").strip()
    if explicit:
        return explicit
    host = (os.getenv("REDIS_HOST", "127.0.0.1") or "127.0.0.1").strip()
    port = int((os.getenv("REDIS_PORT", "6379") or "6379").strip())
    db = int((os.getenv("REDIS_DB", "0") or "0").strip())
    password = os.getenv("REDIS_PASSWORD", "")
    if password:
        return f"redis://:{password}@{host}:{port}/{db}"
    return f"redis://{host}:{port}/{db}"


def _prefixed(key: str) -> str:
    return f"{_PREFIX}:{key}"


def _get_client():
    global _CLIENT, _LAST_FAIL_AT
    if redis is None:
        return None
    if _CLIENT is not None:
        return _CLIENT

    now = time.time()
    if now - _LAST_FAIL_AT < _FAIL_COOLDOWN_SEC:
        return None

    try:
        client = redis.Redis.from_url(
            _redis_url(),
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
            health_check_interval=30,
        )
        client.ping()
        _CLIENT = client
        return _CLIENT
    except Exception as e:
        _LAST_FAIL_AT = now
        _CLIENT = None
        logger.debug("Redis unavailable: %s", e)
        return None


def redis_get_json(key: str) -> Optional[Any]:
    client = _get_client()
    if client is None:
        observe_redis_get("unavailable")
        return None
    try:
        raw = client.get(_prefixed(key))
        if not raw:
            observe_redis_get("miss")
            return None
        parsed = json.loads(raw)
        observe_redis_get("hit")
        return parsed
    except Exception as e:
        logger.debug("Redis GET failed key=%s err=%s", key, e)
        observe_redis_get("error")
        return None


def redis_set_json(key: str, value: Any, ttl_seconds: int) -> bool:
    client = _get_client()
    if client is None:
        observe_redis_set("unavailable")
        return False
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        client.setex(_prefixed(key), max(1, int(ttl_seconds)), payload)
        observe_redis_set("ok")
        return True
    except Exception as e:
        logger.debug("Redis SET failed key=%s err=%s", key, e)
        observe_redis_set("error")
        return False


def redis_claim_once(key: str, value: Any, ttl_seconds: int) -> Optional[bool]:
    """
    Try to claim a key once using Redis SET NX EX.
    Returns:
      True  -> claimed (first caller)
      False -> already claimed by someone else
      None  -> Redis unavailable or error
    """
    client = _get_client()
    if client is None:
        observe_redis_claim("unavailable")
        return None
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        ok = client.set(
            _prefixed(key),
            payload,
            ex=max(1, int(ttl_seconds)),
            nx=True,
        )
        if ok:
            observe_redis_claim("ok")
            return True
        observe_redis_claim("exists")
        return False
    except Exception as e:
        logger.debug("Redis CLAIM failed key=%s err=%s", key, e)
        observe_redis_claim("error")
        return None
