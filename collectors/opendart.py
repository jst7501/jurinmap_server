import requests
from config.settings import DART_API_KEY

class OpenDartCollector:
    def __init__(self):
        self.api_key = DART_API_KEY
        self.base_url = "https://opendart.fss.or.kr/api"
        
    def get_major_shareholders(self, corp_code):
        """
        임원 주요주주 지분변동 수집
        ※ 참고: OpenDART API에서 이 데이터를 요청하려면 회사별 8자리 고유번호(corp_code)가 필요합니다.
        """
        url = f"{self.base_url}/elestock.json"
        
        params = {
            "crtfc_key": self.api_key,
            "corp_code": corp_code 
        }
        
        res = requests.get(url, params=params)
        return res.json()
