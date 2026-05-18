"""
NXT (Next Trade · 대체거래소) 시세 라우터.

KIS inquire-price 의 FID_COND_MRKT_DIV_CODE 옵션으로:
  - "J"  → KRX (정규장 거래소)
  - "NX" → NXT 단독
  - "UN" → 통합 (KRX + NXT 합)

세 값을 동시 조회해 비교 카드를 반환. 단순 in-process 10초 캐시.
정규장 시간(09:00-15:30)엔 NXT 와 KRX 가격이 동일하지만 거래량이 분리됨.
NXT 프리/애프터마켓(08:00-08:50, 15:30-20:00)엔 KRX 정지 상태에서 NXT 만 거래.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, HTTPException

from collectors.kis_api import KISCollector

router = APIRouter(prefix="/api/nxt", tags=["nxt"])
logger = logging.getLogger("server.routes.nxt")

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL_SEC = 10.0

# KIS REST 3개 (J/NX/UN) 동시 호출용 공용 executor.
# - 워커 3 = 동일 종목 3 시장 병렬 (~300ms → ~100ms)
# - max_workers 제한해서 동시성 폭증 방지 (FastAPI 워커가 다중 NXT 호출 시 충돌 회피)
_NXT_EXECUTOR = ThreadPoolExecutor(max_workers=12, thread_name_prefix="nxt-fetch")


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or str(v).strip() in ("", "-"):
            return default
        return int(float(str(v).replace(",", "")))
    except Exception:
        return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() in ("", "-"):
            return default
        return float(str(v).replace(",", ""))
    except Exception:
        return default


def _detect_phase_kst() -> str:
    """한국 시각 기준 NXT phase 분류 (NextTrade 공식 운영시간).
    pre  08:00-09:00 (NXT 프리마켓 — 호가단일가 시간 08:50-09:00 포함, KRX 거래 X)
    regular 09:00-15:30 (KRX·NXT 동시 거래)
    after 15:30-20:00 (NXT 애프터마켓, KRX 거래 X)
    closed 그 외

    ⚠️ 08:50-09:00 갭 수정 (2026-05-08): 이전 pre 상한 08:50 → 09:00 으로 연장.
       이전엔 이 10분간 phase=closed 로 빠져 프론트 폴링 정지 + 캐시 박제 모드로
       전환되며 NXT 프리마켓 활발 거래 구간이 누락됐음.
    """
    from datetime import datetime, timezone, timedelta
    kst = datetime.now(timezone(timedelta(hours=9)))
    m = kst.hour * 60 + kst.minute
    if 8 * 60 <= m < 9 * 60:
        return "pre"
    if 9 * 60 <= m < 15 * 60 + 30:
        return "regular"
    if 15 * 60 + 30 <= m < 20 * 60:
        return "after"
    return "closed"


def _fetch_one(c: KISCollector, code: str, mrkt: str) -> dict:
    res = c._get(
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        {"FID_COND_MRKT_DIV_CODE": mrkt, "FID_INPUT_ISCD": code},
        "FHKST01010100",
    )
    if res.get("rt_cd") != "0":
        return {"error": res.get("error") or "kis_failed"}
    out = res.get("output") or {}
    if not out:
        return {"error": "empty_output"}
    price = _safe_int(out.get("stck_prpr"))
    volume = _safe_int(out.get("acml_vol"))
    payload = {
        "price": price,
        "change_pct": _safe_float(out.get("prdy_ctrt")),
        "change_amt": _safe_int(out.get("prdy_vrss")),
        "open": _safe_int(out.get("stck_oprc")),
        "high": _safe_int(out.get("stck_hgpr")),
        "low": _safe_int(out.get("stck_lwpr")),
        "volume": volume,
        "trading_value": _safe_int(out.get("acml_tr_pbmn")),
    }
    # KIS 가 시간 외 / 미체결 시 모든 값 0 으로 응답하는 경우가 있음.
    # 가격·거래량 모두 0 이면 명시적 no_data 마킹 — 프론트가 phase 라벨로 분기.
    if price <= 0 and volume <= 0:
        payload["error"] = "no_data"
    return payload


@router.get("/{code}")
def get_nxt_price(code: str) -> dict:
    """KRX(J) · NXT(NX) · 통합(UN) 3개 시세를 동시 반환.

    응답:
    {
      "code": "005930",
      "krx":   {"price":..., "change_pct":..., ...},
      "nxt":   {...},
      "total": {...},
      "delta": {                                 # NXT - KRX (NXT 단독 가격 갭)
        "price_diff": ..., "pct_diff": ...,
        "nxt_volume_share": 0.55                 # NXT 거래량 / 통합 거래량
      },
      "fetched_at": 1710000000.0
    }
    """
    code = (code or "").strip()
    # NXT 는 일반 주식만 거래 (6자리 숫자). ETN·스팩·리츠 등 영문 섞인 코드는
    # NXT 미지원이므로 400 대신 200 + nxt.error 로 응답해 프론트가 카드 숨김.
    if not code or len(code) != 6 or not code.isdigit():
        return {
            "code": code,
            "krx": {"error": "unsupported_code"},
            "nxt": {"error": "unsupported_code"},
            "total": {"error": "unsupported_code"},
            "delta": {},
            "fetched_at": time.time(),
            "skipped": True,
        }

    now = time.time()
    cached = _CACHE.get(code)
    if cached and (now - cached[0]) < _CACHE_TTL_SEC:
        return cached[1]

    try:
        c = KISCollector()
    except Exception as exc:
        logger.exception("KIS init failed")
        raise HTTPException(status_code=503, detail=f"kis_init_failed: {exc}")

    # KST phase 기준으로 호출 시장 코드 분기.
    # - regular (09:00-15:30): J/NX/UN 동시 — KRX·NXT 양쪽 모두 거래 중
    # - pre/after (NXT 단독 시간): NX/UN 만 호출. J(KRX) 는 거래 X 라 호출해도 0/no_data
    #   → KIS rate limit 절약 + 프론트가 KRX 칸 "장 외" 라벨 표시
    # - closed: 캐시 만 (프론트 폴링 정지하지만 직접 호출 보호)
    phase = _detect_phase_kst()
    f_nxt = _NXT_EXECUTOR.submit(_fetch_one, c, code, "NX")
    f_total = _NXT_EXECUTOR.submit(_fetch_one, c, code, "UN")
    if phase == "regular":
        f_krx = _NXT_EXECUTOR.submit(_fetch_one, c, code, "J")
        krx = f_krx.result()
    else:
        krx = {"error": "krx_off_hours", "phase": phase}
    nxt = f_nxt.result()
    total = f_total.result()

    # NXT/UN 모두 실패면 503 (KRX 는 시간 외 정상 0)
    if "error" in nxt and "error" in total and ("error" in krx or krx.get("price", 0) == 0):
        raise HTTPException(status_code=503, detail=f"all_kis_failed phase={phase}")

    delta: dict = {}
    if "error" not in krx and "error" not in nxt and krx.get("price") and nxt.get("price"):
        delta["price_diff"] = nxt["price"] - krx["price"]
        if krx["price"]:
            delta["pct_diff"] = round((nxt["price"] - krx["price"]) / krx["price"] * 100, 3)
    if "error" not in nxt and "error" not in total and total.get("volume"):
        delta["nxt_volume_share"] = round(nxt.get("volume", 0) / total["volume"], 3)

    payload = {
        "code": code,
        "phase": phase,  # 프론트 라벨링용
        "krx": krx,
        "nxt": nxt,
        "total": total,
        "delta": delta,
        "fetched_at": now,
    }
    _CACHE[code] = (now, payload)

    # 캐시 prune (1000 종목 넘으면 가장 오래된 200개 제거)
    if len(_CACHE) > 1000:
        oldest = sorted(_CACHE.items(), key=lambda kv: kv[1][0])[:200]
        for k, _ in oldest:
            _CACHE.pop(k, None)

    return payload
