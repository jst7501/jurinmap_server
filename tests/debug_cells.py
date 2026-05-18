import requests
from bs4 import BeautifulSoup

r = requests.get(
    "https://finance.naver.com/item/board.naver?code=010170",
    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"},
    timeout=10
)
soup = BeautifulSoup(r.content, "html.parser", from_encoding="cp949")

# 첫 번째 실제 게시글 행 분석
for row in soup.select("table.type2 tr"):
    td_title = row.select_one("td.title a")
    if not td_title:
        continue
    cells = row.select("td")
    print(f"총 td 수: {len(cells)}")
    for i, cell in enumerate(cells):
        print(f"  cells[{i}] class={cell.get('class')} | text='{cell.text.strip()[:40]}'")
    break
