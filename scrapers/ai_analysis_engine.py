import os
import json
import re
import time
from google import genai
import google.genai.types

class HumanIndicatorAI:
    def __init__(self, api_key=None):
        # 2026-05-18: 하드코딩 키 제거 — .env 의 GEMINI_API_KEY 사용
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        if self.api_key:
            self.client = genai.Client(api_key=self.api_key)
            self.model_id = 'gemini-2.5-flash-lite'
        else:
            self.client = None

    def analyze(self, titles, ticker, stock_name, news_headlines=None):
        """
        종목 토론방 제목 + 최신 뉴스를 분석하여 인간지표 리포트를 생성합니다.
        """
        if not self.client or not titles:
            return None

        titles_to_analyze = titles[:200]
        news_section = ""
        if news_headlines:
            news_lines = "\n".join([f"- {h}" for h in news_headlines])
            news_section = f"""
## 최신 뉴스 헤드라인 (언론 보도 기준, {len(news_headlines)}건)
{news_lines}

뉴스는 '실제 일어난 팩트'이므로 issue_keywords 추출 및 contrarian_signal 판단 시 높은 가중치로 반영해.
토론방 제목의 감정과 뉴스 팩트가 일치하는지, 아니면 과반응/과소반응인지도 평가해줘.
"""

        prompt = f"""너는 주식 시장의 '인간지표'를 분석하는 행동경제학 전문가이자, 텍스트에서 자금 흐름과 모멘텀을 읽어내는 퀀트 애널리스트야.

아래 [{stock_name}] ({ticker}) 종목의 **토론방 제목 {len(titles_to_analyze)}개**와 **최신 뉴스 헤드라인**을 종합해서, 투자자 심리와 주가 흐름의 핵심 이슈를 진단해줘.

---

## [규칙 1] sentiment_keywords — 투자자 감정 단어 (5개)
투자자들이 감정적 극단에 도달했을 때 사용하는 **은어, 욕설, 비속어, 과격한 표현**만 추출해.

가이드라인 (이런 류의 단어를 찾되, 반드시 이 목록에만 한정하지 말 것):
- 고통/공포 계열: 물렸다, 손절, 한강, 반토막, 탈출, 구조대, 주담, 상폐, 사기꾼, 지옥, 폭락, 개박살, 털림, 설거지, 패닉 등
- 탐욕/환희 계열: 가즈아, 풀매수, 대박, 인생역전, 졸업, 쩜상, 텐배거, 영차, 고점, 축제 등

**게시글에서 실제로 많이 등장하는** 감정 표현을 자유롭게 발굴해서 선택해.
객관적 사실(종목명, 가격, 날짜, 공시 용어)은 절대 포함 금지.

## [규칙 2] issue_keywords — 현재 주가 흐름의 실제 이슈 (5개)
게시판을 지배하는 **구체적인 재료, 이벤트, 팩트 키워드**를 추출해.

가이드라인 (이런 류의 단어를 찾되, 이 목록에만 한정하지 말 것):
- 악재: 유상증자, 횡령, 배임, 소송, 실적쇼크, 하한가, 거래정지, 기관매도
- 호재: 수주, 공급계약, 임상성공, FDA승인, 실적서프라이즈, 외국인매수, 상한가
- 테마/이슈: AI, 반도체, 2차전지, 방산, 바이오, 미중관계, 금리, 환율

이 종목만의 특수한 이슈(특정 제품명, 고객사, 파이프라인 등)도 자유롭게 발굴해.

## [규칙 3] 심리 단계 분류
1.무관심(indifference) → 2.기대/의심(expectation) → 3.환희/가즈아(euphoria) →
4.현실부정/물타기(denial) → 5.분노/원망(anger) → 6.체념/자조(capitulation)

## [규칙 4] contrarian_signal — 이슈 본질을 파악해서 역발상 판단
맹목적인 역발상을 피하고, 악재의 심각도를 냉정하게 평가해.
- **BUY** 🟢: 점수 30 이하 극단 공포 + 치명적 악재(횡령·상폐·대규모유증 등)가 없고, 단순 투매/패닉셀로 판단될 때
- **SELL** 🔴: 점수 70 이상 극단 환희 → 단기 고점 징후
- **HOLD** 🟡: 점수 애매하거나, 공포여도 진짜 심각한 악재가 원인이라 매수 금지인 상황

---

## 출력 형식 (JSON만. 마크다운 블록 없이 순수 JSON으로만 답변)
{{
  "ticker": "{ticker}",
  "human_indicator_score": 0에서 100 사이 정수 유동적으로 최대한 정확한 심리를 반영한 점수를 줘야함
  "sentiment_phase": "indifference" | "expectation" | "euphoria" | "denial" | "anger" | "capitulation",
  "sentiment_phase_kor": "한글 단계명",
  "core_issue": "현재 게시판을 지배하는 핵심 이슈 한 줄 요약 (예: 300억 유상증자 쇼크로 기관 투매 → 개미 패닉)",
  "sentiment_keywords": [
    {{"word": "손절", "count": 추정 빈도 정수}},
    {{"word": "한강", "count": 추정 빈도 정수}}
  ],
  "issue_keywords": [
    {{"word": "유상증자", "count": 추정 빈도 정수}},
    {{"word": "임상", "count": 추정 빈도 정수}}
  ],
  "contrarian_signal": "BUY" | "SELL" | "HOLD",
  "contrarian_signal_kor": "판단 근거 한 줄 (예: 패닉이지만 유증 악재 현재진행형 — 하락 칼날 잡기 금지)",
  "summary": "개미 심리 상태와 핵심 이슈를 종합한 역발상 인사이트 2~3문장. 투자자들이 실제로 무엇에 반응하고 있는지, 이 심리가 매수/매도 관점에서 어떤 의미인지 구체적으로 서술해."
}}

---
## 게시글 제목 ({len(titles_to_analyze)}개)
{chr(10).join([f"- {t}" for t in titles_to_analyze])}
{news_section}
"""

        try:
            response = self.client.models.generate_content(
                model=self.model_id,
                contents=prompt
            )
            text = response.text.strip()

            # JSON 추출 (마크다운 백틱 제거)
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                json_str = match.group(0)
                return json.loads(json_str)
            else:
                return json.loads(text)
        except Exception as e:
            print(f"   [!] Gemini AI 분석 실패 ({ticker}): {e}")
            return None

if __name__ == "__main__":
    # 간단 테스트
    ai = HumanIndicatorAI()
    sample_titles = [
        "사기꾼 주담 전화 안받네",
        "오늘도 하한가인가요",
        "한강 수온 체크하러 갑니다",
        "내돈 돌려내라 이놈들아",
        "상폐가 답이다 그냥"
    ]
    result = ai.analyze(sample_titles, "138080", "오이솔루션")
    print(json.dumps(result, indent=2, ensure_ascii=False))
