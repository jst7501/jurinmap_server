import requests
from bs4 import BeautifulSoup

def research():
    code = "138080"
    url = f"https://finance.naver.com/item/board.naver?code={code}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    print(f"Requesting: {url}")
    r = requests.get(url, headers=headers)
    r.encoding = 'cp949'
    
    soup = BeautifulSoup(r.text, 'html.parser')
    
    # 1. <iframe> 태그 모두 출력
    iframes = soup.find_all('iframe')
    print(f"Found {len(iframes)} iframes")
    for i, frame in enumerate(iframes):
        print(f"Iframe {i}: {frame.get('src')}")
        
    # 2. 'type2' 클래스 테이블 찾기
    tables = soup.find_all('table', class_='type2')
    print(f"Found {len(tables)} tables with class 'type2'")
    
    # 3. 직접 '제목' 텍스트 포함된 행 찾기
    titles = soup.select('td.title a')
    print(f"Found {len(titles)} titles via 'td.title a'")
    for t in titles[:5]:
        print(f"  - {t.text.strip()}")

if __name__ == "__main__":
    research()
