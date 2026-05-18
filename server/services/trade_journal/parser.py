"""
토스증권 거래내역서 PDF 파서.

실제 레이아웃 (2026.04 발급 기준):
    거래일자 | 거래구분 | 종목명(종목코드) | 환율 | 거래수량 | 거래대금 |
    단가 | 수수료 | 거래세 | 제세금 | 변제/연체합 | 잔고 | 잔액

거래구분 종류:
    - 구매 / 판매           → 실제 매매 (BUY / SELL)
    - 이체입금 / 이체출금   → 현금흐름 (무시)
    - 이자입금 / 대출       → 현금흐름 (무시)
    - 환전원화입금/출금     → 환전 (무시)
    - 해외주식이벤트입고    → 무시
    - 외화이자입금 등       → 무시

주의사항:
    1) 구매/판매 행은 환율 컬럼이 비어있어 숫자 9개만 나옴
    2) 긴 종목명은 2~3줄로 분할됨 → 버퍼링 필요
    3) 달러 거래내역은 셀마다 원화/달러 이중 표기 → 현재는 원화 금액만 사용
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import List

import pdfplumber

from server.services.trade_journal.models import Trade


_SIDE_MAP = {"구매": "BUY", "판매": "SELL"}
_DATE_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}\b")

# 원화 거래 (환율 컬럼 없음) — 숫자 9개
#  수량 거래대금 단가 수수료 거래세 제세금 변제/연체합 잔고 잔액
_KRW_TRADE_RE = re.compile(
    r"^(?P<date>\d{4}\.\d{2}\.\d{2})\s+"
    r"(?P<type>구매|판매)\s+"
    r"(?P<sym>.+?)"
    r"\((?P<code>[A-Z0-9]+)\)\s+"
    r"(?P<qty>[\d,]+)\s+"
    r"(?P<amount>[\d,]+)\s+"
    r"(?P<price>[\d,]+)\s+"
    r"(?P<fee>[\d,]+)\s+"
    r"(?P<tax>[\d,]+)\s+"
    r"(?P<ptax>[\d,]+)\s+"
    r"(?P<debt>-?[\d,]+)\s+"
    r"(?P<pos>-?[\d,]+)\s+"
    r"(?P<balance>-?[\d,]+)\s*$"
)

# 외화 거래 (환율 컬럼 있음) — 환율 + 숫자 9개 (원화값만, 달러 라인은 continuation으로 들어옴)
_USD_TRADE_RE = re.compile(
    r"^(?P<date>\d{4}\.\d{2}\.\d{2})\s+"
    r"(?P<type>구매|판매)\s+"
    r"(?P<sym>.+?)"
    r"\((?P<code>[A-Z0-9]+)\)\s+"
    r"(?P<rate>[\d,]+\.\d+)\s+"
    r"(?P<qty>[\d,]+(?:\.\d+)?)\s+"
    r"(?P<amount>[\d,]+)\s+"
    r"(?P<price>[\d,]+)\s+"
    r"(?P<fee>[\d,]+)\s+"
    r"(?P<ptax>[\d,]+)\s+"
    r"(?P<debt>-?[\d,]+)\s+"
    r"(?P<pos>-?[\d,]+(?:\.\d+)?)\s+"
    r"(?P<balance>-?[\d,]+)"
)


def _num(s: str) -> float:
    if s is None:
        return 0.0
    s = str(s).replace(",", "").strip()
    if not s or s == "-":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_toss_pdf(path: str, source_file: str | None = None) -> List[Trade]:
    """토스 거래내역서 PDF → Trade 리스트."""
    trades: List[Trade] = []
    in_usd_section = False

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if "달러 거래내역" in text:
                in_usd_section = True  # 한 번 진입하면 이후 페이지도 달러
            trades.extend(_parse_page_text(text, source_file, in_usd_section))
    return trades


_ORPHAN_CODE_RE = re.compile(r"^\s*(?P<tail>[^()\d]*\([A-Z0-9]+\))")


def _splice_orphan_codes(lines: list[str]) -> list[str]:
    """
    pdfplumber가 긴 종목명을 분할하면 '(A251340)' 같은 괄호 코드만 단독 라인으로
    빠지는 경우가 있음. 이전 라인에 이미 (CODE)가 없으면 종목명 뒤에 끼워 넣음.
    """
    out: list[str] = []
    skip_next = False
    for i, line in enumerate(lines):
        if skip_next:
            skip_next = False
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        m = _ORPHAN_CODE_RE.match(nxt.strip())
        if m and not re.search(r"\([A-Z0-9]+\)", line):
            tail = m.group("tail").strip()  # e.g. "(A251340)" 또는 "ETN B(Q530134)"
            # 종목명(구매/판매 뒤~첫 숫자 앞) 끝에 이름 파편+코드 삽입
            new = re.sub(
                r"^(\d{4}\.\d{2}\.\d{2}\s+(?:구매|판매)\s+)(.+?)(\s+[\d,]+)",
                lambda mm: mm.group(1) + mm.group(2).rstrip() + " " + tail + mm.group(3),
                line,
                count=1,
            )
            out.append(new)
            skip_next = True
        else:
            out.append(line)
    return out


def _parse_page_text(text: str, source_file: str | None, usd_mode: bool) -> List[Trade]:
    """
    페이지 텍스트에서 구매/판매 행을 추출.
    긴 종목명 대응: 날짜로 시작하는 줄부터 다음 날짜 줄 직전까지를 한 버퍼로 이어붙임.
    """
    out: List[Trade] = []
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    lines = _splice_orphan_codes(lines)

    buf_parts: list[str] = []

    def flush(parts: list[str]) -> None:
        if not parts:
            return
        joined = " ".join(parts)
        trade = _match_trade(joined, source_file, usd_mode)
        if trade:
            out.append(trade)

    for line in lines:
        if _DATE_RE.match(line):
            flush(buf_parts)
            buf_parts = [line]
        else:
            if buf_parts:
                buf_parts.append(line.strip())
    flush(buf_parts)
    return out


def _match_trade(buf: str, source_file: str | None, usd_mode: bool) -> Trade | None:
    # 달러 섹션의 한 행은 달러 표기까지 모두 한 버퍼에 있음 →
    # 맨 앞의 원화/환율 파트만 보고 매칭되면 OK. 뒤쪽 ($ xxx) 노이즈는 무시.
    if " 구매 " not in buf and " 판매 " not in buf:
        return None

    if usd_mode:
        m = _USD_TRADE_RE.match(buf)
        currency = "USD"
    else:
        m = _KRW_TRADE_RE.match(buf)
        currency = "KRW"

    if not m:
        return None

    try:
        traded_at = datetime.strptime(m.group("date"), "%Y.%m.%d")
    except ValueError:
        return None

    sym = m.group("sym").strip()
    # 버퍼에 줄바꿈으로 끼어있던 여러 공백 제거
    sym = re.sub(r"\s+", " ", sym)

    return Trade(
        traded_at=traded_at,
        symbol=sym,
        ticker=m.group("code"),
        side=_SIDE_MAP[m.group("type")],
        quantity=_num(m.group("qty")),
        price=_num(m.group("price")),
        amount=_num(m.group("amount")),
        fee=_num(m.group("fee")),
        tax=_num(m.group("ptax")) + (_num(m.groupdict().get("tax")) if "tax" in m.groupdict() else 0.0),
        currency=currency,
        source_file=source_file,
    )
