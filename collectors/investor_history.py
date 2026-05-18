"""
investor_history.py — 당일 수급 데이터를 날짜별로 누적 관리

파일 구조 (data/investor_history.json):
{
  "046970": [
    {"date": "20260403", "foreign": 1234, "institution": -567,
     "individual": -667, "etc_org": 0, "program": 100, ...},
    {"date": "20260402", ...},
    ...  (최대 30일치 보관)
  ],
  ...
}
"""

import json
import os
from datetime import datetime

HISTORY_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'investor_history.json')
MAX_HISTORY_DAYS = 30


class InvestorHistory:
    def __init__(self):
        self.path = os.path.abspath(HISTORY_PATH)
        self.data: dict = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"  [InvestorHistory] 로드 실패, 새로 시작: {e}")
        return {}

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    @staticmethod
    def normalize_date(date_str: str, year: str = None) -> str:
        """
        날짜 문자열을 YYYYMMDD 8자리로 정규화.
        지원 형식: "20260403", "04/02" (→ "20260402"), "2026.04.02"
        """
        if not date_str or date_str == '-':
            return date_str
        s = str(date_str).strip().replace('.', '').replace('/', '').replace('-', '')
        if len(s) == 8 and s.isdigit():
            return s
        if len(s) == 4 and s.isdigit():
            y = year or datetime.now().strftime("%Y")
            return f"{y}{s}"
        return date_str

    def update_today(self, stock_code: str, investor_data: dict, date_str: str = None):
        """
        수급 데이터를 히스토리에 추가/갱신.
        date_str: 임의 날짜 형식 (YYYYMMDD/MM-DD 등), None이면 오늘
        """
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")
        date_str = self.normalize_date(date_str)

        if stock_code not in self.data:
            self.data[stock_code] = []

        history = self.data[stock_code]
        existing = next((i for i, r in enumerate(history) if r.get('date') == date_str), None)

        # 핵심 필드만 저장 (히스토리는 용량 절약)
        entry = {
            "date":        date_str,
            "foreign":     int(investor_data.get("foreign", 0) or 0),
            "institution": int(investor_data.get("institution", 0) or 0),
            "individual":  int(investor_data.get("individual", 0) or 0),
            "etc_org":     int(investor_data.get("etc_org", 0) or 0),
            "program":     int(investor_data.get("program", 0) or 0),
            # 매수/매도 거래량 (있으면 저장)
            "foreign_buy":      int(investor_data.get("foreign_buy", 0) or 0),
            "foreign_sell":     int(investor_data.get("foreign_sell", 0) or 0),
            "institution_buy":  int(investor_data.get("institution_buy", 0) or 0),
            "institution_sell": int(investor_data.get("institution_sell", 0) or 0),
            "individual_buy":   int(investor_data.get("individual_buy", 0) or 0),
            "individual_sell":  int(investor_data.get("individual_sell", 0) or 0),
        }

        if existing is not None:
            history[existing] = entry
        else:
            history.insert(0, entry)

        history.sort(key=lambda r: r.get('date', ''), reverse=True)
        self.data[stock_code] = history[:MAX_HISTORY_DAYS]

    def get_5d(self, stock_code: str) -> list:
        """최근 5거래일 수급 반환"""
        return self.get_nd(stock_code, 5)

    def get_nd(self, stock_code: str, n: int = 20) -> list:
        """최근 N거래일 수급 반환"""
        history = self.data.get(stock_code, [])
        return list(history[:n])

    def has_today(self, stock_code: str, date_str: str = None) -> bool:
        """오늘 이미 데이터가 있는지 확인"""
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")
        date_str = self.normalize_date(date_str)
        history = self.data.get(stock_code, [])
        return any(r.get('date') == date_str for r in history)
