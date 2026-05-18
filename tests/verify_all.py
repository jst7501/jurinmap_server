import json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

d = json.load(open('dashboard/public/data.json', encoding='utf-8'))
codes = [k for k in d if k != '_macro']
print('=== 검증 결과 ===')
for code in codes:
    s = d[code]
    bs = s.get('board_sentiment', {})
    ks = s.get('krx_short_balance', {})
    posts = bs.get('total_posts_scanned', 0)
    score = bs.get('score', '-')
    ratio = ks.get('short_ratio', '-')
    mood  = bs.get('mood', '?')
    print(f"[{code}] {s.get('name','?')[:10]:10s} | 종토방 {posts:3}개 {score}점 ({mood}) | 공매도 {ratio}%")

inv = d.get('_macro', {}).get('investor_trading', {})
print(f'\n투자자 유형 ({len(inv)}개): {list(inv.keys())}')
