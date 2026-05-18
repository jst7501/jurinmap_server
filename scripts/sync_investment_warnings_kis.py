"""
sync_investment_warnings_kis.py
───────────────────────────────────────────────────────────────────
KIS 현재가 응답(`price_today.raw_json`)을 파싱해 종목별 시장경보 / 단기과열 /
정리매매 / 관리종목 / 투자유의 단계를 `investment_warnings` 테이블에 upsert.

네이버 시장경보 페이지(`sync_investment_warnings.py`) 와의 차이:
- 네이버: 종목 리스트 + 시세 (사유·지정일 컬럼 **없음**)
- KIS:    각 종목 현재가 응답에 **분류 코드** 포함 → 단계 정확히 분류 가능
- 사유 텍스트는 KIS 도 없음. 단계만 채움.

KIS 응답 필드 (inquire_price TR):
- `mrkt_warn_cls_code`: '00' 정상 / '01' 투자주의 / '02' 투자경고 / '03' 투자위험
- `short_over_yn`:      'Y' 단기과열 지정
- `sltr_yn`:            'Y' 정리매매 지정
- `mang_issu_cls_code`: '00' 외 → 관리종목
- `invt_caful_yn`:      'Y' 투자유의 (ETF/ETN 등)

스키마:
  investment_warnings(code, warning_type, designated_date, reason, note, updated_at)
  PK: (code, warning_type)

운영:
- price_today refresh 직후 호출 (cron 또는 scheduled-tasks)
- 같은 PK 충돌 시 reason 정보가 더 풍부한 행을 보존하도록 INSERT … ON CONFLICT 처리.
  (네이버 trading_halt 의 reason 은 풍부 → 보존)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Any

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)


from server.db.connections import get_stocks_conn  # noqa: E402


# KIS mrkt_warn_cls_code → warning_type 매핑
WARN_CODE_MAP = {
    "01": "caution",
    "02": "warning",
    "03": "risk",
}


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_raw_json(raw: Any) -> dict[str, Any] | None:
    """price_today.raw_json 이 string 이거나 dict 일 수 있음."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


def _extract_kis_flags(raw: dict[str, Any]) -> list[tuple[str, str]]:
    """raw_json 에서 (warning_type, reason) tuple 리스트 추출.

    하나의 종목이 여러 단계(예: 관리종목 + 단기과열) 동시 가능.
    reason 은 KIS 코드가 알려주는 fact 만 (단계명).
    """
    out: list[tuple[str, str]] = []

    mwc = (raw.get("mrkt_warn_cls_code") or "").strip()
    if mwc in WARN_CODE_MAP:
        wt = WARN_CODE_MAP[mwc]
        # 사유: KIS 는 텍스트 없음 → 단계 라벨만 기록 (네이버 trading_halt reason 처럼 풍부하진 않음)
        label = {"caution": "투자주의 지정", "warning": "투자경고 지정", "risk": "투자위험 지정"}[wt]
        out.append((wt, label))

    if (raw.get("short_over_yn") or "").strip().upper() == "Y":
        # 단기과열은 별도 분류 — caution 으로 합치되 reason 으로 구분
        out.append(("caution", "단기과열 종목 지정"))

    if (raw.get("sltr_yn") or "").strip().upper() == "Y":
        # 정리매매: trading_halt 에 가까우나 별도 의미. trading_halt 로 분류 + reason 명시
        out.append(("trading_halt", "정리매매 종목 (상장폐지 절차)"))

    if (raw.get("mang_issu_cls_code") or "").strip().upper() == "Y":
        # 관리종목 → 우리 스키마 4단계 중 trading_halt 로 분류 + reason 명시
        out.append(("trading_halt", "관리종목 지정"))

    if (raw.get("invt_caful_yn") or "").strip().upper() == "Y":
        out.append(("caution", "투자유의 종목 (ETF·ETN)"))

    return out


def main() -> int:
    conn = get_stocks_conn()
    try:
        rows = conn.execute(
            "SELECT code, raw_json FROM price_today WHERE raw_json IS NOT NULL"
        ).fetchall()

        ts = now_ts()
        # KIS 출처로 만든 row (code, warning_type, reason) 모음 — 중복 제거
        kis_rows: dict[tuple[str, str], str] = {}  # (code, warning_type) → reason
        for r in rows:
            d = dict(r)
            code = d.get("code")
            raw = _parse_raw_json(d.get("raw_json"))
            if not code or not raw:
                continue
            for wt, reason in _extract_kis_flags(raw):
                # 같은 (code, warning_type) 중복은 더 풍부한 reason 우선 (긴 텍스트 보존)
                key = (code, wt)
                cur = kis_rows.get(key, "")
                if len(reason) > len(cur):
                    kis_rows[key] = reason

        # KIS 가 더 이상 단계로 간주하지 않는 종목은 해제됨 → 옛 KIS-origin 행 정리
        # 단, 네이버에서 들어온 trading_halt 의 진짜 사유 텍스트는 보존해야 함.
        # 식별: note 컬럼에 'KIS' 표시한 행만 KIS-origin 으로 간주하고 별도 관리.

        written = 0
        # 1) 모든 KIS-origin 기존 행 삭제 (note 가 'KIS' prefix 인 것만)
        # db_compat 의 _qmark_to_pyformat 가 SQL 안의 % 를 placeholder 로 오해하므로
        # LIKE 'KIS%' 직접 쓰지 말고 param 으로 전달
        conn.execute(
            "DELETE FROM investment_warnings WHERE note LIKE ?",
            ("KIS%",),
        )

        # 2) 새 KIS 행 일괄 insert
        for (code, wt), reason in kis_rows.items():
            note = f"KIS:{wt}"
            conn.execute(
                """
                INSERT INTO investment_warnings(code, warning_type, designated_date, reason, note, updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(code, warning_type) DO UPDATE SET
                  reason = CASE
                    WHEN investment_warnings.reason IS NULL OR investment_warnings.reason = ''
                    THEN excluded.reason
                    ELSE investment_warnings.reason
                  END,
                  note      = CASE
                    WHEN substring(investment_warnings.note FROM 1 FOR 3) = 'KIS'
                    THEN excluded.note
                    ELSE investment_warnings.note
                  END,
                  updated_at = excluded.updated_at
                """,
                (code, wt, "", reason, note, ts),
            )
            written += 1

        conn.commit()
        print(f"[sync_investment_warnings_kis] processed price_today rows: {len(rows)}, kis-origin rows: {written}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
