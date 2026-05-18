import pandas as pd
import json

# 컬럼 확인
df1 = pd.read_csv('투자자별순매수.csv', encoding='cp949', nrows=5)
df2 = pd.read_csv('개별공매도.csv', encoding='cp949', nrows=5)

result = {
    "investor_cols": df1.columns.tolist(),
    "investor_sample": df1.head(3).to_dict(orient='records'),
    "short_cols": df2.columns.tolist(),
    "short_sample": df2.head(3).to_dict(orient='records'),
}

with open('data/csv_structure.json', 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

print("저장 완료: data/csv_structure.json")
