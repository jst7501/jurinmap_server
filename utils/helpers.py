import os
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# 토큰 파일명 (날짜 무관하게 고정 이름으로 관리)
_TOKEN_FILE = "KIS_TOKEN"
# 하위 호환: 기존 파일명도 fallback으로 탐색
_LEGACY_TOKEN_FILES = ["KIS20260406", "KIS20260403"]

_cached_token: str | None = None
_cached_expiry: datetime | None = None


def _root_dir() -> str:
    return os.path.dirname(os.path.dirname(__file__))


def _read_token_file(path: str) -> tuple[str | None, datetime | None]:
    """파일에서 (token, valid_datetime) 파싱. 실패 시 (None, None)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            token, expiry = None, None
            for line in f:
                line = line.strip()
                if line.startswith("token:"):
                    token = line.split("token:", 1)[1].strip()
                elif line.startswith("valid-date:"):
                    raw = line.split("valid-date:", 1)[1].strip()
                    try:
                        expiry = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        pass
            return token, expiry
    except Exception:
        return None, None


def _write_token_file(token: str, expiry: datetime) -> None:
    path = os.path.join(_root_dir(), _TOKEN_FILE)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"token: {token}\nvalid-date: {expiry.strftime('%Y-%m-%d %H:%M:%S')}\n")
    logger.info("[KIS] 토큰 파일 갱신 → %s (만료: %s)", _TOKEN_FILE, expiry)


def _refresh_token() -> str | None:
    """KIS OAuth2 토큰 재발급. 성공 시 파일 저장 + 캐시 갱신."""
    global _cached_token, _cached_expiry
    try:
        import requests
        from config.settings import KIS_APP_KEY, KIS_APP_SECRET, KIS_DOMAIN
        if not KIS_APP_KEY or not KIS_APP_SECRET:
            logger.warning("[KIS] APP_KEY / APP_SECRET 미설정 — 자동 갱신 불가")
            return None

        url = f"{KIS_DOMAIN}/oauth2/tokenP"
        payload = {
            "grant_type": "client_credentials",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
        }
        res = requests.post(url, json=payload, timeout=10)
        data = res.json()

        if "access_token" not in data:
            logger.error("[KIS] 토큰 발급 실패: %s", data)
            return None

        token = data["access_token"]
        # 만료시각 파싱 (KIS: "YYYY-MM-DD HH:MM:SS" 또는 초단위 정수)
        raw_exp = data.get("access_token_token_expired", "")
        try:
            expiry = datetime.strptime(str(raw_exp), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            expiry = datetime.now() + timedelta(days=1)

        _cached_token = token
        _cached_expiry = expiry
        _write_token_file(token, expiry)
        logger.info("[KIS] 토큰 자동 갱신 성공 (만료: %s)", expiry)
        return token

    except Exception as e:
        logger.error("[KIS] 토큰 갱신 중 예외: %s", e)
        return None


def get_kis_token() -> str:
    """
    KIS 액세스 토큰 반환.
    1) 메모리 캐시 (만료 5분 전까지 유효)
    2) KIS_TOKEN 파일
    3) 레거시 파일 (KIS20260406 등)
    4) .env KIS_ACCESS_TOKEN
    만료됐거나 없으면 자동 재발급 시도.
    """
    global _cached_token, _cached_expiry

    now = datetime.now()
    margin = timedelta(minutes=5)  # 만료 5분 전부터 선제 갱신

    # 1. 메모리 캐시
    if _cached_token and _cached_expiry and now < _cached_expiry - margin:
        return _cached_token

    # 2. KIS_TOKEN 파일
    root = _root_dir()
    token_path = os.path.join(root, _TOKEN_FILE)
    token, expiry = _read_token_file(token_path)

    # 3. 레거시 파일 fallback
    if not token:
        for legacy in _LEGACY_TOKEN_FILES:
            t, e = _read_token_file(os.path.join(root, legacy))
            if t:
                token, expiry = t, e
                break

    # 파일 토큰이 유효하면 사용
    if token and expiry and now < expiry - margin:
        _cached_token = token
        _cached_expiry = expiry
        return token

    # 만료 또는 없음 → 자동 갱신 시도
    if token:
        logger.warning("[KIS] 토큰 만료됨 (만료: %s) — 자동 갱신 시도", expiry)
    else:
        logger.warning("[KIS] 토큰 파일 없음 — 자동 갱신 시도")

    refreshed = _refresh_token()
    if refreshed:
        return refreshed

    # 갱신 실패 시 .env fallback (만료됐더라도 일단 사용)
    if token:
        logger.warning("[KIS] 갱신 실패 — 만료된 토큰으로 재시도")
        _cached_token = token
        _cached_expiry = expiry
        return token

    try:
        from config.settings import KIS_ACCESS_TOKEN
        if KIS_ACCESS_TOKEN:
            return KIS_ACCESS_TOKEN
    except Exception:
        pass

    logger.error("[KIS] 사용 가능한 토큰 없음")
    return ""
