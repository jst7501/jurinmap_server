import os
from pykrx import stock
import json
from datetime import datetime, timedelta

def main():
    print("▶ KRX(한국거래소) 전체 종목 공매도 잔고 현황 크롤러 시작")
    print("  (법적 공시 딜레이로 인해 T-2~T-5일전 데이터를 자동 탐색합니다.)")
    
    target_date = None
    target_df = None
    
    # 최근 5영업일 중 공매도 잔고 데이터가 채워진 최신일 탐색
    for i in range(2, 7):
        d = (datetime.now() - timedelta(days=i)).strftime('%Y%m%d')
        try:
            print(f"  - {d} 일자 조회 중...")
            df = stock.get_shorting_balance_by_ticker(d)
            print(df)
            if df is not None and not df.empty:
                target_date = d
                target_df = df
                print(f"✅ {d} 일자 데이터 발견 성공 (총 {len(df)}개 종목)")
                break
        except Exception as e:
            print(e)
            pass
            
    if target_df is None:
        print("❌ 실패: 최근 5일 이내에 공매도 잔고 데이터가 존재하지 않거나 서버 연결에 실패했습니다.")
        return
        
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "short_sale_balance_latest.json")
    csv_path = os.path.join(out_dir, "short_sale_balance_latest.csv")
    
    # pykrx dataframe을 딕셔너리로 (index가 티커(종목코드))
    result_map = {}
    for code, row in target_df.iterrows():
        # pykrx의 get_shorting_balance_by_ticker 컬럼: 공매도잔고수량, 공매도잔고금액, 상장주식수대비비중, 비고 등
        qty = row.get("공매도잔고수량", 0)
        amt = row.get("공매도잔고금액", 0)
        ratio = row.get("상장주식수대비비중", 0)
        
        result_map[code] = {
            "short_balance_qty": int(qty) if qty == qty else 0,
            "short_balance_amt": int(amt) if amt == amt else 0,
            "short_ratio": float(ratio) if ratio == ratio else 0.0,
            "date": target_date
        }
        
    # JSON 직렬화
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(result_map, f, ensure_ascii=False, indent=2)
        
    # 활용을 위해 CSV도 같이 떨궈줌
    target_df.to_csv(csv_path, encoding='utf-8-sig')
    
    print(f"\n💾 수집 완료!")
    print(f"  - JSON 저장 위치: {out_path}")
    print(f"  - CSV 저장 위치: {csv_path}")
    
if __name__ == "__main__":
    main()
