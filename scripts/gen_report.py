import sys
import os

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import json

d = json.load(open(os.path.join(ROOT_DIR, 'dashboard', 'public', 'data.json'), encoding='utf-8'))
codes = [k for k in d if k != '_macro']
lines = ['=== 검증 결과 ===']
for code in codes:
    s = d[code]
    bs = s.get('board_sentiment', {})
    ks = s.get('krx_short_balance', {})
    lines.append(
        f"[{code}] {s.get('name','?')[:12]:12s}"
        f" | 종토방 {bs.get('total_posts_scanned',0):3}개"
        f" {bs.get('score','-'):>3}점 ({bs.get('mood','?')})"
        f" | 공매도잔고비중 {ks.get('short_ratio','-')}%"
    )

inv = d.get('_macro', {}).get('investor_trading', {})
lines.append(f"\n투자자 유형 ({len(inv)}개): {list(inv.keys())}")
if inv:
    for k, v in inv.items():
        lines.append(f"  {k:10s}: 순매수 {v.get('net_qty','?')} 주  / {v.get('net_amt','?')} 원")

report = '\n'.join(lines)
out_path = os.path.join(ROOT_DIR, 'data', 'pipeline_report.txt')
with open(out_path, 'w', encoding='utf-8') as f:
    f.write(report)
print(f"리포트 저장: {out_path}")
