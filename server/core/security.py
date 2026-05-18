"""API access guards for HTTP and WebSocket endpoints."""

from __future__ import annotations

import os
import re
from typing import Optional
from urllib.parse import urlparse

from fastapi import Request, WebSocket

_DEFAULT_ALLOWED_ORIGINS = [
    "https://jurinmap.com",
    "https://www.jurinmap.com",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _parse_csv(name: str) -> list[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _normalize_origin(value: str) -> str:
    return str(value or "").strip().rstrip("/").lower()


def _extract_hostname(value: str) -> str:
    if not value:
        return ""
    try:
        return (urlparse(value).hostname or "").strip().lower()
    except Exception:
        return ""


def _build_allowed_origins() -> list[str]:
    api_origins = _parse_csv("API_ALLOW_ORIGINS")
    if api_origins:
        return [_normalize_origin(item) for item in api_origins]
    cors_origins = _parse_csv("CORS_ALLOW_ORIGINS")
    if cors_origins:
        return [_normalize_origin(item) for item in cors_origins]
    return [_normalize_origin(item) for item in _DEFAULT_ALLOWED_ORIGINS]


def _build_allowed_referer_hosts(allowed_origins: list[str]) -> set[str]:
    configured = {item.strip().lower() for item in _parse_csv("API_ALLOW_REFERER_HOSTS") if item.strip()}
    if configured:
        return configured
    inferred: set[str] = set()
    for origin in allowed_origins:
        host = _extract_hostname(origin)
        if host:
            inferred.add(host)
    return inferred


_ALLOWED_ORIGINS = set(_build_allowed_origins())
_ALLOWED_ORIGIN_REGEX_RAW = (
    os.getenv("API_ALLOW_ORIGIN_REGEX", "").strip()
    or os.getenv("CORS_ALLOW_ORIGIN_REGEX", "").strip()
)
_ALLOWED_ORIGIN_REGEX = re.compile(_ALLOWED_ORIGIN_REGEX_RAW) if _ALLOWED_ORIGIN_REGEX_RAW else None
_ALLOWED_REFERER_HOSTS = _build_allowed_referer_hosts(list(_ALLOWED_ORIGINS))

API_SHARED_KEY = os.getenv("API_SHARED_KEY", "").strip()
API_OPEN_MODE = _env_bool("API_OPEN_MODE", True)
API_ENFORCE_WEB_ORIGIN = _env_bool("API_ENFORCE_WEB_ORIGIN", False)
API_BLOCK_NO_ORIGIN = _env_bool("API_BLOCK_NO_ORIGIN", False)
API_REQUIRE_HTTP_KEY = _env_bool("API_REQUIRE_HTTP_KEY", False)
API_REQUIRE_WS_KEY = _env_bool("API_REQUIRE_WS_KEY", False)


def _is_allowed_origin(origin: str) -> bool:
    normalized = _normalize_origin(origin)
    if not normalized or normalized == "null":
        return False
    if normalized in _ALLOWED_ORIGINS:
        return True
    if _ALLOWED_ORIGIN_REGEX is not None and _ALLOWED_ORIGIN_REGEX.match(normalized):
        return True
    return False


def _is_allowed_referer(referer: str) -> bool:
    host = _extract_hostname(referer)
    if not host:
        return False
    return host in _ALLOWED_REFERER_HOSTS


def _extract_api_key(headers, query_params) -> str:
    header_key = str(headers.get("x-api-key") or "").strip()
    if header_key:
        return header_key

    auth = str(headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        bearer = auth[7:].strip()
        if bearer:
            return bearer

    query_key = str(query_params.get("api_key") or "").strip()
    if query_key:
        return query_key
    return ""


def verify_http_request(request: Request) -> tuple[bool, Optional[str]]:
    if API_OPEN_MODE:
        return True, None

    method = request.method.upper()
    if method == "OPTIONS":
        return True, None

    if API_SHARED_KEY and API_REQUIRE_HTTP_KEY:
        provided = _extract_api_key(request.headers, request.query_params)
        if provided != API_SHARED_KEY:
            return False, "invalid_api_key"

    if not API_ENFORCE_WEB_ORIGIN:
        return True, None

    origin = str(request.headers.get("origin") or "").strip()
    referer = str(request.headers.get("referer") or "").strip()

    if origin:
        ok = _is_allowed_origin(origin)
        return (ok, None if ok else "origin_not_allowed")
    if referer:
        ok = _is_allowed_referer(referer)
        return (ok, None if ok else "referer_not_allowed")
    if API_BLOCK_NO_ORIGIN:
        return False, "missing_origin"
    return True, None


def verify_websocket_request(websocket: WebSocket) -> tuple[bool, Optional[str]]:
    if API_OPEN_MODE:
        return True, None

    if API_SHARED_KEY and API_REQUIRE_WS_KEY:
        provided = _extract_api_key(websocket.headers, websocket.query_params)
        if provided != API_SHARED_KEY:
            return False, "invalid_api_key"

    if not API_ENFORCE_WEB_ORIGIN:
        return True, None

    origin = str(websocket.headers.get("origin") or "").strip()
    referer = str(websocket.headers.get("referer") or "").strip()

    if origin:
        ok = _is_allowed_origin(origin)
        return (ok, None if ok else "origin_not_allowed")
    if referer:
        ok = _is_allowed_referer(referer)
        return (ok, None if ok else "referer_not_allowed")
    if API_BLOCK_NO_ORIGIN:
        return False, "missing_origin"
    return True, None


async def reject_websocket_if_unauthorized(websocket: WebSocket) -> bool:
    ok, reason = verify_websocket_request(websocket)
    if ok:
        return False
    await websocket.close(code=1008, reason=(reason or "forbidden")[:120])
    return True


def security_runtime_summary() -> dict:
    return {
        "open_mode": API_OPEN_MODE,
        "enforce_web_origin": API_ENFORCE_WEB_ORIGIN,
        "block_no_origin": API_BLOCK_NO_ORIGIN,
        "api_key_enabled": bool(API_SHARED_KEY),
        "require_http_key": API_REQUIRE_HTTP_KEY,
        "require_ws_key": API_REQUIRE_WS_KEY,
        "allowed_origins": sorted(_ALLOWED_ORIGINS),
        "allowed_origin_regex": _ALLOWED_ORIGIN_REGEX_RAW or None,
        "allowed_referer_hosts": sorted(_ALLOWED_REFERER_HOSTS),
    }
