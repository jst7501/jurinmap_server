import requests, json
from bs4 import BeautifulSoup

r = requests.get(
    "https://finance.naver.com/item/board.naver?code=010170",
    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"},
    timeout=10
)
soup = BeautifulSoup(r.content, "html.parser", from_encoding="cp949")

result = []
for row in soup.select("table.type2 tr"):
    td_title = row.select_one("td.title a")
    if not td_title:
        continue
    cells = row.select("td")
    row_info = [(i, cell.get("class"), cell.text.strip()[:40]) for i, cell in enumerate(cells)]
    result.append(row_info)
    if len(result) >= 3:
        break

with open("data/cell_debug.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print("saved to data/cell_debug.json")
