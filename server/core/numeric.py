"""
server/core/numeric.py — 숫자 변환 공통 유틸 (2026-05-18 통합)

기존에 _safe_float / _to_float / _safe_int / _to_float_safe 등이 9개 파일에
제각각 12벌 산재해 있던 것을 통합. 두 시맨틱(None 폴백 / default 폴백)을
default 인자 하나로 흡수한다.

상위호환 설계 — 콤마 제거 + nan/None/빈문자/'-' 처리를 모두 포함하므로
기존 어느 변형보다 robust. 기존 호출부를 그대로 두고 정의만
`from server.core.numeric import to_float as _safe_float` 식으로 교체 가능.
"""
from typing import Any, Optional

_NULLISH = ("", "-", "none", "nan", "null", "n/a")


def to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """문자열·Decimal·None 등을 float 로 정규화. 실패 시 default.

    - 콤마 제거 ("1,234" → 1234.0)
    - 빈 문자열·'-'·'nan'·'None'·'null'·'n/a' → default
    - Postgres Decimal 등도 float() 로 흡수
    """
    if value is None:
        return default
    try:
        if isinstance(value, (int, float)):
            f = float(value)
        else:
            s = str(value).strip().replace(",", "")
            if s.lower() in _NULLISH:
                return default
            f = float(s)
    except (ValueError, TypeError):
        return default
    # nan / inf 방어
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


def to_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """to_float 경유 후 int 캐스팅. 실패 시 default."""
    f = to_float(value, None)
    if f is None:
        return default
    try:
        return int(f)
    except (ValueError, TypeError, OverflowError):
        return default
