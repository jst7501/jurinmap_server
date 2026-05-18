import json
import os
from collectors.kis_api import KISCollector
from datetime import datetime

def update_top_ranking():
    """
    KIS API를 사용하여 코스피/코스닥 거래대금 상위 100 종목 데이터를 수집하고 저장합니다.
    """
    kis = KISCollector()
    
    print(f"[{datetime.now()}] 거래대금 상위 종목 수집 시작...")
    
    # 1. 코스피 상위 100 (0001)
    print("  KOSPI 상위 100 수집 중...")
    kospi_top = kis.get_transaction_value_ranking("0001")
    
    # 2. 코스닥 상위 100 (1001)
    print("  KOSDAQ 상위 100 수집 중...")
    kosdaq_top = kis.get_transaction_value_ranking("1001")
    
    # 데이터 구성
    data = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "kospi": kospi_top,
        "kosdaq": kosdaq_top
    }
    
    # 저장 경로 확인 및 파일 저장
    data_dir = "data"
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
        
    file_path = os.path.join(data_dir, "top_100_trade_value.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        
    print(f"  수집 완료! 저장 경로: {file_path}")
    print(f"  - KOSPI: {len(kospi_top)} 종목")
    print(f"  - KOSDAQ: {len(kosdaq_top)} 종목")

if __name__ == "__main__":
    update_top_ranking()
