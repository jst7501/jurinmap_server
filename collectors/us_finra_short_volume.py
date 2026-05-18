"""FINRA Reg SHO Daily Short Sale Volume — 매일 EOD CSV.

URL: https://cdn.finra.org/equity/regsho/daily/CNMS shvolYYYYMMDD.txt
컬럼: Date | Symbol | ShortVolume | ShortExemptVolume | TotalVolume | Market

CNMS = Consolidated NMS (NYSE/NASDAQ/AMEX 등 통합).
BPRT = BATS, OTCS = OTC. 우리는 CNMS 우선.

매일 EOD 직후 발표 → T+1 가까운 신선도. "어제 거래의 몇 %가 공매도였나"라는
급등주 트레이더 핵심 지표. short_volume_ratio = ShortVolume / TotalVolume.
0.5 이상이면 매도세 공매도 위주 → squeeze 텐션 누적 신호.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import requests

_LOGGER = logging.getLogger(__name__)
_BASE_URL = "https://cdn.finra.org/equity/regsho/daily/{kind}shvol{date}.txt"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
}


def _fetch_text(kind: str, date_yyyymmdd: str, timeout: int = 20) -> Optional[str]:
    url = _BASE_URL.format(kind=kind, date=date_yyyymmdd)
    try:
        r = requests.get(url, headers=_HEADERS, timeout=timeout)
        if r.status_code != 200:
            return None
        # 첫 줄에 header가 있어야 valid
        if "ShortVolume" not in r.text[:200]:
            return None
        return r.text
    except Exception:
        return None


def fetch_latest_available(kinds: Iterable[str] = ("CNMS",), lookback_days: int = 5) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """FINRA 가 발표한 가장 최근 일자 데이터 가져오기. 미국 ET 기준 어제~5일전 까지 탐색.

    Returns: (date_yyyymmdd, kind, text) — 셋 다 같이. 실패 시 (None,None,None).
    """
    # ET 기준 어제부터 lookback_days 일 까지 역순 시도
    today_et = datetime.now(timezone.utc) - timedelta(hours=4)  # EDT 휴리스틱
    for d in range(lookback_days):
        dt = (today_et - timedelta(days=d)).strftime("%Y%m%d")
        for kind in kinds:
            text = _fetch_text(kind, dt)
            if text:
                return dt, kind, text
    return None, None, None


def parse(text: str) -> list[dict]:
    """파이프 구분 CSV 파싱. 마지막 한두 줄은 'File Created' 같은 푸터라 스킵."""
    out: list[dict] = []
    reader = csv.DictReader(io.StringIO(text), delimiter="|")
    for row in reader:
        sym = (row.get("Symbol") or "").strip()
        if not sym or sym.startswith("File") or "|" in sym:
            continue
        try:
            short_vol = float(row.get("ShortVolume") or 0)
            short_exempt = float(row.get("ShortExemptVolume") or 0)
            total_vol = float(row.get("TotalVolume") or 0)
        except (ValueError, TypeError):
            continue
        if total_vol <= 0:
            continue
        ratio = (short_vol + short_exempt) / total_vol
        out.append({
            "symbol": sym,
            "date": row.get("Date") or "",
            "short_volume": short_vol,
            "short_exempt_volume": short_exempt,
            "total_volume": total_vol,
            "short_volume_ratio": round(ratio, 4),  # 0.0 ~ 1.0
            "market": (row.get("Market") or "").strip(),  # B,Q,N 등 ECN code
        })
    return out


def get_today_short_volume(min_ratio: float = 0.0, min_volume: int = 0) -> list[dict]:
    """가장 최근 영업일 FINRA short volume. 필터 옵션 포함.

    Args:
        min_ratio: 최소 short_volume_ratio (예: 0.5 = 매도의 50%+ 가 공매도)
        min_volume: 최소 total_volume (필터 noise 종목 제거)
    """
    date_str, kind, text = fetch_latest_available()
    if not text:
        return []
    rows = parse(text)
    if min_ratio > 0:
        rows = [r for r in rows if r["short_volume_ratio"] >= min_ratio]
    if min_volume > 0:
        rows = [r for r in rows if r["total_volume"] >= min_volume]
    rows.sort(key=lambda r: r["short_volume_ratio"], reverse=True)
    return rows


if __name__ == "__main__":
    date_str, kind, text = fetch_latest_available()
    print(f"latest available: {date_str} kind={kind}")
    if not text:
        print("no data")
        raise SystemExit(1)
    rows = parse(text)
    print(f"total rows: {len(rows):,}")
    # TOP 10 short ratio (volume > 100K to filter dust)
    hot = [r for r in rows if r["total_volume"] >= 100_000]
    hot.sort(key=lambda r: r["short_volume_ratio"], reverse=True)
    print(f"\n=== TOP 10 short volume ratio (TotalVol >= 100K) ===")
    for r in hot[:10]:
        print(f"  {r['symbol']:<6}  ratio={r['short_volume_ratio']:.1%}  total={int(r['total_volume']):>12,}  short={int(r['short_volume']):>12,}")
    # 핵심 종목들의 값
    print(f"\n=== Key tickers ===")
    by_sym = {r["symbol"]: r for r in rows}
    for sym in ["TSLA", "NVDA", "AAPL", "GME", "AMC", "RIVN", "PLTR", "MARA", "COIN", "MSTR", "HOOD", "SMCI", "RDDT"]:
        r = by_sym.get(sym)
        if r:
            print(f"  {sym:<6}  ratio={r['short_volume_ratio']:.1%}  total={int(r['total_volume']):>12,}  short={int(r['short_volume']):>12,}")
