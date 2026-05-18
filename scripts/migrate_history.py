"""기존 investor_history.json의 날짜 형식을 YYYYMMDD로 일괄 정규화"""
import sys
import os

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

import json
from collectors.investor_history import InvestorHistory

h = InvestorHistory()
changed = 0
for code in list(h.data.keys()):
    new_rows = []
    for row in h.data[code]:
        orig = row.get('date', '')
        norm = InvestorHistory.normalize_date(orig)
        if norm != orig:
            row['date'] = norm
            changed += 1
        new_rows.append(row)
    # 다시 정렬
    new_rows.sort(key=lambda r: r.get('date', ''), reverse=True)
    h.data[code] = new_rows

h.save()
print(f"날짜 정규화 완료: {changed}건 변경")

# 결과 출력
for code, rows in h.data.items():
    line = f'{code}: {len(rows)}일치'
    if rows:
        line += f' | 날짜={[r["date"] for r in rows[:3]]}'
    print(f'  {line}')
