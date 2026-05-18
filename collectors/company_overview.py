"""
회사 기업개요 수집기.

소스:
- fnguide c1020001 (wisereport): 본사주소·홈페이지·설립·종업원수·연혁·매출구성·내수수출·R&D
- fnguide SVD_Corp: 자회사 사업 설명·계열명

주린이용 종합 요약을 LLM에 넣을 fact 재료를 모아서 dict로 반환.
"""

from __future__ import annotations

import re
from typing import Any

import requests
from bs4 import BeautifulSoup


_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


class CompanyOverviewCollector:
    def __init__(self, timeout: int = 12):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(_HEADERS)

    # ------------------------------------------------------------------ utils
    def _get(self, url: str) -> str | None:
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code != 200:
                return None
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception:
            return None

    @staticmethod
    def _clean(text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    # ------------------------------------------------------------- wisereport
    def fetch_wisereport(self, code: str) -> dict[str, Any]:
        """fnguide의 wisereport c1020001 페이지에서 사실 재료 추출."""
        url = (
            "https://navercomp.wisereport.co.kr/v2/company/c1020001.aspx"
            f"?cmp_cd={code}"
        )
        html = self._get(url)
        if not html:
            return {"source": "wisereport", "ok": False, "error": "fetch_failed"}
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
        text_clean = self._clean(text)

        facts: dict[str, Any] = {"source": "wisereport", "ok": True}

        # 종목 영문/시장/업종
        m = re.search(
            r"(KOSPI|KOSDAQ|KONEX)\s*:\s*(\S+)\s+(\S+)\s+WICS\s*:\s*(\S+)",
            text_clean,
        )
        if m:
            facts["market"] = m.group(1)
            facts["sector"] = m.group(3)
            facts["wics"] = m.group(4)

        # 본사주소
        m = re.search(r"본사주소\s+([^\s홈페이지]+(?:\s[^\s홈페이지]+){0,8})", text_clean)
        if m:
            facts["address"] = m.group(1).strip()

        # 홈페이지
        m = re.search(r"홈페이지\s+(https?://[^\s]+)", text_clean)
        if m:
            facts["homepage"] = m.group(1)

        # 설립일·상장일
        m = re.search(r"설립일\s+(\d{4}/\d{2}/\d{2})\s*\(?상장일\s*[:：]?\s*(\d{4}/\d{2}/\d{2})", text_clean)
        if m:
            facts["founded"] = m.group(1)
            facts["listed"] = m.group(2)
        else:
            m1 = re.search(r"설립일\s+(\d{4}/\d{2}/\d{2})", text_clean)
            if m1:
                facts["founded"] = m1.group(1)
            m2 = re.search(r"상장일\s*[:：]?\s*(\d{4}/\d{2}/\d{2})", text_clean)
            if m2:
                facts["listed"] = m2.group(1)

        # 대표이사
        m = re.search(r"대표이사\s+([가-힣A-Za-z·\s]{2,40}?)\s+(?:계열|종업원수|발행주식수)", text_clean)
        if m:
            facts["ceo"] = m.group(1).strip()

        # 종업원수
        m = re.search(r"종업원수\s+(\d{1,6})", text_clean)
        if m:
            facts["employees"] = int(m.group(1))

        # 발행주식수
        m = re.search(r"발행주식수\(보통/우선\)\s+([\d,]+)\s*주", text_clean)
        if m:
            try:
                facts["shares_common"] = int(m.group(1).replace(",", ""))
            except ValueError:
                pass

        # 최근연혁 (최대 6개)
        history = []
        for hm in re.finditer(
            r"(\d{4}/\d{2})\s+([^0-9][^/]{2,200}?)(?=\s+\d{4}/\d{2}\s+[^0-9]|\s+주요제품|\s+신용등급|\s+자본금)",
            text_clean,
        ):
            history.append({"date": hm.group(1), "text": self._clean(hm.group(2))[:200]})
            if len(history) >= 6:
                break
        if history:
            facts["history"] = history

        # 주요제품 매출구성 (제품명 + 구성비)
        revenue_mix = []
        # "주요제품 매출구성 ... 제품명 구성비 ... <제품명> <%> ..." 형태
        rmm = re.search(
            r"주요제품 매출구성.*?제품명\s+구성비(.*?)(?:차트 건너뛰기|연구개발비|인원 현황|내수)",
            text_clean,
        )
        if rmm:
            seg = rmm.group(1)
            # "<상품/제품명> <숫자.숫자> ..." 패턴 — 한글/영문/공백/괄호 허용
            for pm in re.finditer(
                r"([가-힣A-Za-z][가-힣A-Za-z0-9\s\(\)·,/\-]{1,40}?)\s+(\d{1,3}\.\d{1,2})(?=\s|$)",
                seg,
            ):
                name = self._clean(pm.group(1))
                pct = float(pm.group(2))
                if name and 0 <= pct <= 100:
                    revenue_mix.append({"name": name, "pct": pct})
                if len(revenue_mix) >= 8:
                    break
        if revenue_mix:
            facts["revenue_mix"] = revenue_mix

        # 내수/수출 구성 (제품별 내수%/수출%)
        export_mix = []
        em = re.search(
            r"내수 및 수출구성.*?매출유형(.*?)(?:신용등급|자본금|당사 매출은)",
            text_clean,
        )
        if em:
            seg = em.group(1)
            for pm in re.finditer(
                r"제품\s+([가-힣A-Za-z][가-힣A-Za-z0-9\s\(\)·,/\-]{1,40}?)\s+([\d\.]+)\s+([\d\.]+)(?=\s+제품|\s+기타|\s+신용|\s+자본|$)",
                seg,
            ):
                try:
                    export_mix.append({
                        "name": self._clean(pm.group(1)),
                        "domestic_pct": float(pm.group(2)),
                        "export_pct": float(pm.group(3)),
                    })
                except ValueError:
                    continue
                if len(export_mix) >= 8:
                    break
        if export_mix:
            facts["export_mix"] = export_mix

        return facts

    # --------------------------------------------------------- fnguide.SVD_Corp
    def fetch_subsidiaries(self, code: str) -> list[dict[str, Any]]:
        """SVD_Corp 페이지에서 자회사·사업 설명 추출."""
        url = f"https://comp.fnguide.com/SVO2/ASP/SVD_Corp.asp?pGB=1&gicode=A{code}"
        html = self._get(url)
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        text = self._clean(soup.get_text(" ", strip=True))

        subs = []
        # "<영문/한글 회사명> <사업설명> <YYYY/MM> <지분%>" 패턴 시도
        # 더 안정적으로: '주요사업' 다음 ~ '* 연결대상' 까지 잘라서 단순 토큰화
        m = re.search(r"주요사업[^a-zA-Z가-힣]*(.*?)\*\s*연결대상", text)
        if not m:
            return []
        seg = m.group(1)
        # 패턴: "회사명 [영/한] 사업설명 YYYY/MM 숫자"
        for pm in re.finditer(
            r"([A-Za-z가-힣][A-Za-z가-힣0-9·\(\)\.\-,& ]{2,60}?)\s+([^0-9]{3,80}?)\s+(\d{4}/\d{2})\s+([\d\.]+)",
            seg,
        ):
            subs.append({
                "name": self._clean(pm.group(1)),
                "business": self._clean(pm.group(2)),
                "since": pm.group(3),
                "stake_or_assets": pm.group(4),
            })
            if len(subs) >= 10:
                break
        return subs

    # ------------------------------------------------------------------- main
    def collect(self, code: str) -> dict[str, Any]:
        wise = self.fetch_wisereport(code)
        subs = self.fetch_subsidiaries(code)
        out: dict[str, Any] = {"code": code, "wisereport": wise}
        if subs:
            out["subsidiaries"] = subs
        return out


if __name__ == "__main__":
    import json
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    c = CompanyOverviewCollector()
    for code in ["321260", "005930", "035720"]:
        data = c.collect(code)
        print(f"==== {code} ====")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
        print()
