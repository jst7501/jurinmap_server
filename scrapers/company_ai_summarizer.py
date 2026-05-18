"""
주린이용 종합 기업 요약 생성기.

입력: code, name, 기존 한 줄 요약, fnguide overview, 소속 테마
출력: JSON {one_liner, business_summary, products, revenue_mix, sector,
            themes, investor_point, full_summary}

기존 ai_analysis_engine.py의 Gemini 클라이언트 패턴을 재사용.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from google import genai

class CompanyAISummarizer:
    def __init__(self, api_key: str | None = None, model_id: str = "gemini-2.5-flash-lite"):
        # 2026-05-18: 하드코딩 _DEFAULT_KEY 제거 — .env 의 GEMINI_API_KEY 사용
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.client = genai.Client(api_key=self.api_key)
        self.model_id = model_id

    @staticmethod
    def _format_facts(name: str, code: str, seed_oneline: str | None,
                      overview: dict[str, Any], themes: list[str]) -> str:
        wise = (overview or {}).get("wisereport") or {}
        subs = (overview or {}).get("subsidiaries") or []
        lines: list[str] = []
        lines.append(f"종목명: {name} ({code})")
        if seed_oneline:
            lines.append(f"기존 한 줄: {seed_oneline}")
        if wise.get("market"):
            lines.append(f"시장: {wise['market']}")
        if wise.get("sector") or wise.get("wics"):
            lines.append(f"업종: {wise.get('sector') or ''} / WICS: {wise.get('wics') or ''}")
        if wise.get("homepage"):
            lines.append(f"홈페이지: {wise['homepage']}")
        if wise.get("founded"):
            lines.append(f"설립: {wise['founded']} (상장: {wise.get('listed', '?')})")
        if wise.get("employees"):
            lines.append(f"종업원수: {wise['employees']}명")
        if wise.get("ceo"):
            lines.append(f"대표이사: {wise['ceo']}")
        if wise.get("revenue_mix"):
            mix = ", ".join(f"{m['name']} {m['pct']}%" for m in wise["revenue_mix"])
            lines.append(f"매출구성: {mix}")
        if wise.get("export_mix"):
            xs = wise["export_mix"]
            lines.append("내수/수출 (제품별):")
            for x in xs:
                lines.append(f"  - {x['name']}: 내수 {x['domestic_pct']}% / 수출 {x['export_pct']}%")
        if wise.get("history"):
            lines.append("최근연혁:")
            for h in wise["history"][:5]:
                lines.append(f"  - {h['date']}: {h['text'][:100]}")
        if subs:
            lines.append("자회사·관계사:")
            for s in subs[:6]:
                lines.append(f"  - {s.get('name','')}: {s.get('business','')}")
        if themes:
            lines.append(f"소속 테마: {', '.join(themes[:8])}")
        return "\n".join(lines)

    def summarize(self, code: str, name: str, seed_oneline: str | None,
                  overview: dict[str, Any], themes: list[str] | None = None) -> dict[str, Any] | None:
        themes = themes or []
        facts = self._format_facts(name, code, seed_oneline, overview, themes)

        prompt = f"""너는 주식 초보자(주린이)에게 한국 상장사를 친근하게 설명하는 AI 애널리스트야.
아래 [팩트]만 근거로 사용해서, 절대 사실을 지어내지 마. 정보가 부족하면 그 항목은 빈 문자열 ""로 둬.

---
[팩트]
{facts}
---

다음 JSON 스키마로만 답해. 마크다운 코드블록 없이 순수 JSON.

{{
  "one_liner": "한 문장 (40자 이내). '○○를 만드는 ○○ 회사' 형태. 친근체 가능.",
  "business_summary": "이 회사가 무엇을 만들고 누구에게 파는지 2~3문장 (150자 이내). 주린이도 알 수 있게 쉬운 단어로.",
  "products": "주력 제품/서비스를 콤마로 구분 (예: 'OLED 검사장비, 프로브카드, 유지보수')",
  "revenue_mix": "매출 비중을 한 줄로 (예: '디스플레이 검사장비 99% / 유지보수 1%'). 데이터 없으면 빈 문자열",
  "sector": "업종을 한 줄로 (예: '코스닥 디스플레이장비')",
  "themes": "소속 테마/모멘텀 키워드 콤마 구분 (없으면 빈 문자열)",
  "investor_point": "주린이가 알아야 할 핵심 포인트 한 줄 (예: '디스플레이 투자 사이클에 매출이 출렁이는 부품주야')",
  "full_summary": "위 항목을 종합한 자연스러운 본문 4~6문장 (250~450자). 첫 문장은 '○○는 ~ 회사야.' 로 시작. AI 티 안 나게 자연스럽게."
}}

규칙:
- 모든 값은 한국어
- **톤 일관: 반드시 모든 문장 끝맺음을 '~야 / ~지 / ~어' 같은 친근한 반말로 통일. '~요 / ~답니다 / ~습니다 / ~네요 / ~죠 / ~답니다' 절대 사용 금지.**
- one_liner도 '~야' 또는 명사형(예: '○○ 만드는 회사') 으로 끝낼 것
- full_summary 끝에 '투자권유 아니에요' 같은 군더더기 금지
- '든든한', '믿음직한' 같은 광고성 형용사 자제 — 사실 위주로
- 이모지 사용 금지
- 팩트에 없는 숫자/날짜/제품명 절대 만들지 말 것
- 정보가 너무 부족하면 one_liner와 business_summary만 채우고 나머지는 ""로 둬
"""

        try:
            response = self.client.models.generate_content(
                model=self.model_id,
                contents=prompt,
            )
            text = (response.text or "").strip()
            # JSON 추출
            m = re.search(r"\{.*\}", text, re.DOTALL)
            raw = m.group(0) if m else text
            data = json.loads(raw)
            # 필드 보장
            for k in ["one_liner", "business_summary", "products", "revenue_mix",
                      "sector", "themes", "investor_point", "full_summary"]:
                data.setdefault(k, "")
                if not isinstance(data[k], str):
                    data[k] = str(data[k])
            return data
        except Exception as e:
            return {"_error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    from collectors.company_overview import CompanyOverviewCollector

    collector = CompanyOverviewCollector()
    summarizer = CompanyAISummarizer()

    test_cases = [
        ("321260", "프로이천", "디스플레이 검사장비를 만드는 회사", ["디스플레이"]),
        ("005930", "삼성전자", "메모리 반도체와 스마트폰을 만드는 종합 IT 기업", ["반도체"]),
    ]
    for code, name, seed, themes in test_cases:
        ov = collector.collect(code)
        out = summarizer.summarize(code, name, seed, ov, themes)
        print(f"==== {code} {name} ====")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        print()
