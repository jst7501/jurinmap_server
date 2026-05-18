"""SEC EDGAR Fail-to-Deliver (FTD) 데이터 수집기.

Reg SHO 결제실패 데이터 — Threshold Securities 의 underlying signal.
파일 단위: 반월 (a=1~15일, b=16~말일). 파일 URL 패턴:
  https://www.sec.gov/files/data/fails-deliver-data/cnsfailsYYYYMMx.zip
  (x = 'a' 또는 'b')

Schema (pipe-delimited):
  SETTLEMENT DATE | CUSIP | SYMBOL | QUANTITY (FAILS) | DESCRIPTION | PRICE

데이터 freshness: T+1 약 2주 지연 (예: 4월 1~15일 데이터는 4월 말 공개).
"""
from __future__ import annotations

import io
import logging
import zipfile
from datetime import date, datetime, timedelta
from typing import Iterator, Optional

import requests

logger = logging.getLogger("collectors.us_ftd_sec")

_BASE_URL = "https://www.sec.gov/files/data/fails-deliver-data/cnsfails{ym}{half}.zip"
_HEADERS = {
    "User-Agent": "JurinMapBot research@example.com",  # SEC requires UA with email
    "Accept-Encoding": "gzip, deflate",
}


def _safe_int(v) -> Optional[int]:
    try:
        s = str(v).strip().replace(",", "")
        if not s or s == "0":
            return 0 if s == "0" else None
        return int(s)
    except (TypeError, ValueError):
        return None


def _safe_float(v) -> Optional[float]:
    try:
        s = str(v).strip().replace(",", "").replace("$", "")
        if not s:
            return None
        f = float(s)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def fetch_half_month(year: int, month: int, half: str) -> bytes:
    """반월 zip 파일 다운로드.

    Args:
      year: 2024 / 2025 / 2026
      month: 1~12
      half: 'a' (1~15일) 또는 'b' (16~말일)

    Returns: zip raw bytes
    Raises: RuntimeError if 404 / fetch failure
    """
    assert half in ("a", "b"), f"half must be 'a' or 'b', got {half}"
    ym = f"{year:04d}{month:02d}"
    url = _BASE_URL.format(ym=ym, half=half)
    try:
        r = requests.get(url, headers=_HEADERS, timeout=60)
    except Exception as exc:
        raise RuntimeError(f"network error: {exc}")
    if r.status_code == 404:
        raise RuntimeError(f"FTD file not available: {ym}{half}")
    r.raise_for_status()
    if len(r.content) < 100:
        raise RuntimeError(f"FTD file too small ({len(r.content)} bytes): {ym}{half}")
    return r.content


def parse_zip(zip_bytes: bytes) -> Iterator[dict]:
    """zip 파일 → 행 단위 dict iterator.

    Yields:
      {
        "settlement_date": "2026-04-01",   # ISO
        "cusip": "G29018101",
        "symbol": "DLO",
        "fail_quantity": 14383,
        "description": "DLOCAL LTD COM CL A (CYM)",
        "price": 12.97
      }
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except Exception as exc:
        raise RuntimeError(f"zip parse failed: {exc}")

    for name in zf.namelist():
        try:
            raw = zf.read(name)
        except Exception:
            continue
        text = raw.decode("latin-1", errors="replace")
        lines = text.split("\n")
        if not lines:
            continue
        # 헤더 검증
        header = lines[0].strip()
        if "SYMBOL" not in header.upper() or "QUANTITY" not in header.upper():
            logger.warning("unexpected FTD header: %s", header[:120])
            continue

        for line in lines[1:]:
            parts = line.strip().split("|")
            if len(parts) < 6:
                continue
            try:
                date_raw = parts[0].strip()  # YYYYMMDD
                if len(date_raw) != 8 or not date_raw.isdigit():
                    continue
                yield {
                    "settlement_date": f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}",
                    "cusip": parts[1].strip() or None,
                    "symbol": parts[2].strip().upper() or None,
                    "fail_quantity": _safe_int(parts[3]),
                    "description": parts[4].strip()[:200] or None,
                    "price": _safe_float(parts[5]),
                }
            except Exception:
                continue


def get_ftd_for_period(year: int, month: int, half: str) -> list[dict]:
    """반월 FTD 데이터 전체 list. 작은 파일이라 메모리 OK (보통 50K~200K 행)."""
    raw = fetch_half_month(year, month, half)
    return [r for r in parse_zip(raw) if r.get("symbol") and r.get("fail_quantity")]


def get_recent_months(months_back: int = 2) -> list[tuple[int, int, str]]:
    """최근 N개월의 (year, month, half) 튜플 list, 신선한 것부터."""
    today = date.today()
    targets: list[tuple[int, int, str]] = []
    # 현재 월의 'a' (오늘이 16일 이후이면 'b' 도 가능하나, SEC 공개는 보통 월말 + 2주)
    for offset in range(months_back + 1):
        # offset = 0 ~ months_back
        y = today.year
        m = today.month - offset
        while m <= 0:
            m += 12
            y -= 1
        # 같은 월에 b 가 a 보다 최신
        targets.append((y, m, "b"))
        targets.append((y, m, "a"))
    return targets


def fetch_latest_available(months_back: int = 2) -> dict:
    """최신 가용 FTD 파일 1개 찾아서 데이터 반환.

    Returns:
      {
        "year": 2026, "month": 4, "half": "a",
        "data": [...],   # symbol-level rows
        "row_count": 12345
      }
    """
    for y, m, h in get_recent_months(months_back):
        try:
            rows = get_ftd_for_period(y, m, h)
            if rows:
                return {"year": y, "month": m, "half": h, "data": rows, "row_count": len(rows)}
        except RuntimeError as exc:
            logger.debug("FTD %d%02d%s not available: %s", y, m, h, exc)
            continue
    raise RuntimeError(f"no FTD file found in last {months_back} months")


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) >= 4:
        y, m, h = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
        rows = get_ftd_for_period(y, m, h)
        print(f"period={y}-{m:02d}{h} rows={len(rows)}")
        # symbol별 sum top 10
        from collections import Counter
        sym_sum = Counter()
        for r in rows:
            if r["fail_quantity"]:
                sym_sum[r["symbol"]] += r["fail_quantity"]
        for sym, q in sym_sum.most_common(10):
            print(f"  {sym:8} {q:>15,}")
    else:
        res = fetch_latest_available()
        print(f"latest: {res['year']}-{res['month']:02d}{res['half']} ({res['row_count']:,} rows)")
        from collections import Counter
        sym_sum = Counter()
        for r in res["data"]:
            if r["fail_quantity"]:
                sym_sum[r["symbol"]] += r["fail_quantity"]
        print("top 15 symbols by total fail quantity:")
        for sym, q in sym_sum.most_common(15):
            print(f"  {sym:8} {q:>15,}")
