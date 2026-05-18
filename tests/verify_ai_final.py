import json

with open('dashboard/public/data.json', encoding='utf-8') as f:
    d = json.load(f)

for code in list(d.keys())[:12]:
    if code == "_macro": continue
    stock = d[code]
    bs = stock.get("board_sentiment", {})
    ai = bs.get("ai_insight", {})
    if ai:
        print(f"[{code}] {stock.get('name','?')}: {ai.get('sentiment_phase_kor')} | SIGNAL: {ai.get('contrarian_signal')}")
    else:
        print(f"[{code}] {stock.get('name','?')}: AI Insight MISSING")
