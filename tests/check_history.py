import json

# 수급 히스토리 확인
with open('data/investor_history.json', 'r', encoding='utf-8') as f:
    h = json.load(f)

print('=== 수급 히스토리 ===')
for code, rows in h.items():
    print(f'{code}: {len(rows)}일치 | 최신={rows[0]["date"] if rows else "-"}')
    for r in rows[:3]:
        f_val = r.get("foreign", 0)
        inst_val = r.get("institution", 0)
        ind_val = r.get("individual", 0)
        print(f'  {r["date"]}: 외인={f_val:+,} 기관={inst_val:+,} 개인={ind_val:+,}')
