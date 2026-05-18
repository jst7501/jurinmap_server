from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from server.core.settings import ROOT_DIR

DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DART_MAX_DAYS_NO_CORP_CODE = 90
DEFAULT_GUIDE_FILE_NAME = "ipo_beginner_guide_ko.json"
IPO_INCLUDE_KEYWORDS = (
    "증권신고서(지분증권)",
    "정정증권신고서(지분증권)",
    "투자설명서",
    "정정투자설명서",
    "소액공모",
)
IPO_EXCLUDE_KEYWORDS = (
    "합병",
    "분할",
    "자기주식",
)

DEFAULT_IPO_GUIDE: dict[str, Any] = {
    "version": 1,
    "language": "ko-KR",
    "updated_at": "2026-04-14",
    "intro": (
        "공모주 청약은 상장 전에 일반 투자자가 주식을 미리 신청하는 절차입니다. "
        "핵심은 일정 확인, 증거금 준비, 주관 증권사 계좌 보유입니다."
    ),
    "terms": [
        {
            "term": "공모가",
            "meaning": "회사가 상장 전에 투자자에게 받는 기준 가격",
            "action_tip": "공모가가 높아 보이면 무리해서 큰 금액을 넣지 말고 배정 전략을 먼저 보세요.",
        },
        {
            "term": "청약증거금",
            "meaning": "청약 신청 시 임시로 넣어두는 돈(보통 청약금의 일부)",
            "action_tip": "청약 시작 전에 계좌 현금이 충분한지 먼저 확인하세요.",
        },
        {
            "term": "균등배정",
            "meaning": "최소 청약 수량 이상 신청자에게 비교적 고르게 배정",
            "action_tip": "소액 투자자는 균등 물량 비중이 높은지 꼭 확인하세요.",
        },
        {
            "term": "비례배정",
            "meaning": "증거금을 많이 넣을수록 더 많이 배정받는 방식",
            "action_tip": "비례 경쟁률이 높으면 큰 증거금을 넣어도 실배정이 적을 수 있습니다.",
        },
        {
            "term": "경쟁률",
            "meaning": "신청 수요가 공급 물량보다 얼마나 많은지 보여주는 지표",
            "action_tip": "단순 고경쟁률만 보지 말고 상장 후 유통물량도 함께 보세요.",
        },
        {
            "term": "환불일",
            "meaning": "미배정 또는 초과 납입된 증거금이 다시 들어오는 날짜",
            "action_tip": "환불일까지 자금이 묶이므로 동시 청약 일정과 자금 계획을 같이 잡으세요.",
        },
    ],
    "where_to_apply": [
        {
            "channel": "대표 주관 증권사 MTS/HTS",
            "description": "대부분의 일반 청약은 주관 증권사 앱/HTS에서 진행",
            "how_to_apply": "해당 증권사 계좌 개설 후 공모주/청약 메뉴에서 신청",
        },
        {
            "channel": "공동 주관/인수 증권사",
            "description": "종목마다 신청 가능한 증권사가 다를 수 있음",
            "how_to_apply": "공모 공고에서 청약 가능 증권사를 확인 후 해당 앱에서 신청",
        },
        {
            "channel": "공시 사이트 확인",
            "description": "정확한 일정과 증권사 정보의 원문 확인 용도",
            "how_to_apply": "DART 공시 원문(증권신고서/투자설명서)에서 청약 일정과 주관사 확인",
        },
    ],
    "steps": [
        {
            "step": 1,
            "title": "청약 가능 증권사 확인",
            "description": "DART 공시에서 주관사/공동주관사를 확인하고 내 계좌가 있는지 점검",
        },
        {
            "step": 2,
            "title": "청약 일정 캘린더 등록",
            "description": "청약일, 배정일, 환불일, 상장일을 일정표에 저장",
        },
        {
            "step": 3,
            "title": "증거금/수수료 준비",
            "description": "계좌 현금 잔고에서 증거금과 청약 수수료를 감당 가능한지 확인",
        },
        {
            "step": 4,
            "title": "청약 신청 실행",
            "description": "증권사 앱에서 수량 입력 후 신청하고 접수 결과를 확인",
        },
        {
            "step": 5,
            "title": "배정 결과 확인",
            "description": "배정 수량, 환불 금액, 상장일 매도/보유 계획을 확정",
        },
    ],
    "checklist": [
        "청약 전날 계좌 현금 잔고 확인",
        "주관 증권사 앱 로그인/보안매체 점검",
        "최소 청약 수량 및 수수료 확인",
        "환불일까지 자금 묶임 고려",
        "상장일 변동성 대응 계획(익절/손절) 사전 설정",
    ],
    "caution": [
        "공모주라고 항상 수익이 나는 것은 아닙니다.",
        "상장일 급등 후 급락(변동성 확대) 가능성이 큽니다.",
        "한 종목 집중 대신 자금 분산과 손실 한도 관리가 중요합니다.",
    ],
}


class IpoServiceError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class IpoService:
    def __init__(self) -> None:
        self._cache_dir = Path(ROOT_DIR) / "data" / "ipo"
        self._cache_file = self._cache_dir / "dart_ipo_latest.json"
        self._guide_file = self._cache_dir / DEFAULT_GUIDE_FILE_NAME
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def status(self) -> dict[str, Any]:
        cache = self._load_cache()
        api_key = self._get_dart_api_key()
        return {
            "has_dart_api_key": bool(api_key),
            "cache_path": str(self._cache_file),
            "cached_count": int(cache.get("count", 0)) if cache else 0,
            "last_fetched_at": cache.get("fetched_at") if cache else None,
            "guide_path": str(self._guide_file),
            "has_guide": self._guide_file.exists(),
            "source": "dart",
        }

    def get_beginner_guide(self) -> dict[str, Any]:
        cached = self._load_guide()
        if cached:
            return cached
        return self.save_beginner_guide(DEFAULT_IPO_GUIDE)

    def save_beginner_guide(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._guide_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return payload

    async def get_events(
        self,
        *,
        refresh: bool = False,
        days: int = DART_MAX_DAYS_NO_CORP_CODE,
        page_count: int = 100,
    ) -> dict[str, Any]:
        if refresh:
            return await self.fetch_and_cache(days=days, page_count=page_count)
        cached = self._load_cache()
        if cached:
            return cached
        return await self.fetch_and_cache(days=days, page_count=page_count)

    async def fetch_and_cache(
        self,
        *,
        days: int = DART_MAX_DAYS_NO_CORP_CODE,
        page_count: int = 100,
    ) -> dict[str, Any]:
        safe_days = max(1, min(days, DART_MAX_DAYS_NO_CORP_CODE))
        events = await self._fetch_from_dart(days=safe_days, page_count=page_count)
        payload = {
            "source": "dart",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "days": safe_days,
            "count": len(events),
            "events": events,
        }
        self._cache_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return payload

    async def _fetch_from_dart(
        self,
        *,
        days: int = DART_MAX_DAYS_NO_CORP_CODE,
        page_count: int = 100,
    ) -> list[dict[str, Any]]:
        api_key = self._get_dart_api_key()
        if not api_key:
            raise IpoServiceError("DART_API_KEY is not configured", status_code=400)

        safe_days = max(1, min(days, DART_MAX_DAYS_NO_CORP_CODE))
        safe_page_count = max(10, min(page_count, 100))
        end_dt = datetime.now(timezone.utc)
        begin_dt = end_dt - timedelta(days=safe_days)

        params_base = {
            "crtfc_key": api_key,
            "bgn_de": begin_dt.strftime("%Y%m%d"),
            "end_de": end_dt.strftime("%Y%m%d"),
            "sort": "date",
            "sort_mth": "desc",
            "page_count": str(safe_page_count),
        }

        events: list[dict[str, Any]] = []
        seen_receipts: set[str] = set()

        async with httpx.AsyncClient(timeout=20.0) as client:
            for page in range(1, 51):
                params = {**params_base, "page_no": str(page)}
                response = await client.get(DART_LIST_URL, params=params)
                if response.status_code != 200:
                    raise IpoServiceError(
                        f"DART request failed with status {response.status_code}",
                        status_code=502,
                    )

                try:
                    data = response.json()
                except ValueError as exc:
                    raise IpoServiceError("DART returned invalid JSON", status_code=502) from exc

                status = data.get("status")
                message = data.get("message", "")
                if status == "013":
                    break
                if status != "000":
                    raise IpoServiceError(f"DART API error {status}: {message}", status_code=502)

                items = data.get("list") or []
                if not items:
                    break

                for item in items:
                    if not self._is_ipo_related(str(item.get("report_nm", ""))):
                        continue
                    receipt_no = str(item.get("rcept_no", "")).strip()
                    if not receipt_no or receipt_no in seen_receipts:
                        continue
                    seen_receipts.add(receipt_no)
                    events.append(self._normalize(item))

                if len(items) < safe_page_count:
                    break

        events.sort(key=lambda row: row.get("receipt_date", ""), reverse=True)
        return events

    @staticmethod
    def _is_ipo_related(report_name: str) -> bool:
        if not report_name:
            return False
        included = any(keyword in report_name for keyword in IPO_INCLUDE_KEYWORDS)
        excluded = any(keyword in report_name for keyword in IPO_EXCLUDE_KEYWORDS)
        return included and not excluded

    @staticmethod
    def _normalize(item: dict[str, Any]) -> dict[str, Any]:
        receipt_no = str(item.get("rcept_no", "")).strip()
        receipt_date_raw = str(item.get("rcept_dt", "")).strip()
        if len(receipt_date_raw) == 8:
            receipt_date = f"{receipt_date_raw[0:4]}-{receipt_date_raw[4:6]}-{receipt_date_raw[6:8]}"
        else:
            receipt_date = receipt_date_raw
        return {
            "corp_name": item.get("corp_name", ""),
            "corp_code": item.get("corp_code", ""),
            "stock_code": item.get("stock_code", ""),
            "corp_class": item.get("corp_cls", ""),
            "report_name": item.get("report_nm", ""),
            "receipt_no": receipt_no,
            "receipt_date": receipt_date,
            "filer_name": item.get("flr_nm", ""),
            "dart_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt_no}" if receipt_no else "",
        }

    def _load_cache(self) -> dict[str, Any] | None:
        if not self._cache_file.exists():
            return None
        try:
            return json.loads(self._cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _load_guide(self) -> dict[str, Any] | None:
        if not self._guide_file.exists():
            return None
        try:
            return json.loads(self._guide_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _get_dart_api_key() -> str:
        load_dotenv()
        return (os.getenv("DART_API_KEY") or os.getenv("OPEN_DART_API_KEY") or "").strip()
