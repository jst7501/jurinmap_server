import requests
from bs4 import BeautifulSoup
import re
from collections import Counter

class NaverThemeCollector:
    def __init__(self):
        self.headers = {"User-Agent": "Mozilla/5.0"}
        self.theme_map = {}

    def fetch_themes(self, target_stocks):
        """
        간략하게 테마 페이지를 스크랩하여 target_stocks에 해당하는 종목의 테마들을 매핑합니다.
        베타버전: 네이버 금융의 테마 페이지 상위 5페이지 정도를 스캔하여 매핑
        """
        # (주의: 실제로는 수십 페이지이므로, 전체를 모으려면 반복문이 필요합니다)
        for page in range(1, 4):
            url = f"https://finance.naver.com/sise/theme.naver?&page={page}"
            res = requests.get(url, headers=self.headers)
            soup = BeautifulSoup(res.text, "html.parser")
            
            for tr in soup.select("table.type_1 tr"):
                tds = tr.find_all("td")
                if len(tds) > 1:
                    theme_a = tds[0].find("a")
                    if theme_a:
                        theme_name = theme_a.text.strip()
                        theme_link = "https://finance.naver.com" + theme_a["href"]
                        # 테마 상세 페이지 진입 (부하가 크므로 베타 버전에서는 생략하거나 선택적 적용 가능하지만, 다 수집 목표이므로 실행)
                        self._fetch_theme_stocks(theme_name, theme_link, target_stocks)

        return self.theme_map

    def _fetch_theme_stocks(self, theme_name, url, target_stocks):
        res = requests.get(url, headers=self.headers)
        soup = BeautifulSoup(res.text, "html.parser")
        
        for a in soup.select("div.name_area > a"):
            stock_code = a['href'].split('code=')[-1]
            if stock_code in target_stocks:
                if stock_code not in self.theme_map:
                    self.theme_map[stock_code] = []
                if theme_name not in self.theme_map[stock_code]:
                    self.theme_map[stock_code].append(theme_name)


class NaverNewsCollector:
    def __init__(self):
        self.headers = {"User-Agent": "Mozilla/5.0"}

    def get_keywords(self, stock_name):
        """
        임시 형태소 분석(TF-IDF 대체): 네이버 뉴스 검색 페이지에서 명사를 추출해 빈도수 계산
        """
        url = f"https://search.naver.com/search.naver?where=news&query={stock_name}"
        res = requests.get(url, headers=self.headers)
        soup = BeautifulSoup(res.text, "html.parser")
        
        # 기사 제목들 수집
        titles = [a.text for a in soup.select("a.news_tit")]
        
        # 아주 간단한 형태소/명사 추출 모방 (띄어쓰기 기준 + 조사 제거 등)
        words = []
        for title in titles:
            # 특수문자 제거
            clean_title = re.sub(r'[^가-힣a-zA-Z\s]', '', title)
            for word in clean_title.split():
                if len(word) >= 2 and word != stock_name:
                    words.append(word)
        
        # 빈도수 측정
        counter = Counter(words)
        return dict(counter.most_common(5))

