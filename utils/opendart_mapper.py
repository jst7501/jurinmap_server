import requests
import zipfile
import io
import xml.etree.ElementTree as ET
import os
from config.settings import DART_API_KEY, DATA_DIR

class DartCorpMapper:
    def __init__(self):
        self.api_key = DART_API_KEY
        self.map_file = os.path.join(DATA_DIR, 'corp_code_map.json')
        self.mapping = {}
        
    def _download_and_parse(self):
        url = "https://opendart.fss.or.kr/api/corpCode.xml"
        res = requests.get(url, params={"crtfc_key": self.api_key})
        if res.status_code != 200:
            print("DART corp_code 다운로드 실패")
            return
            
        with zipfile.ZipFile(io.BytesIO(res.content)) as z:
            with z.open('CORPCODE.xml') as f:
                tree = ET.parse(f)
                root = tree.getroot()
                for lst in root.findall('list'):
                    corp_code = lst.find('corp_code').text
                    # DART의 stock_code는 빈칸인 경우도 있음 (상장사가 아닌 경우)
                    stock_code = lst.find('stock_code').text
                    if stock_code and stock_code.strip():
                        self.mapping[stock_code.strip()] = corp_code
                        
    def get_corp_code(self, stock_code):
        if not self.mapping:
            self._download_and_parse()
        return self.mapping.get(stock_code)
