import json, glob, os

files = glob.glob('data/stock_data_*.json')
if not files:
    print('수집 파일 없음')
    exit()

latest = max(files, key=os.path.getmtime)
print(f'파일: {latest}')

with open(latest, 'r', encoding='utf-8') as f:
    d = json.load(f)

codes = [k for k in d if k != '_macro']
print(f'종목 수: {len(codes)}')
for code in codes:
    sd = d[code]
    pt = sd.get('price_today', {})
    raw = pt.get('_raw', {})
    short_ok = raw.get('ssts_yn', 'N')
    cr = sd.get('credit_data', {}).get('rate_today', 0)
    short_ratio = sd.get('short_data', {}).get('short_selling_volume_ratio', 0)
    name = sd.get('name', code)
    print(f'  {name}({code}) | 공매도={short_ok} | 신용={cr}% | 공매비중={short_ratio}%')
