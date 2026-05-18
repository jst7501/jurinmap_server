import sys
import os
import json
import time
from datetime import datetime
import requests
import zipfile
import io
import xml.etree.ElementTree as ET

# 프로젝트 루트 경로 추가
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from config.settings import DART_API_KEY
from collectors.kis_api import KISCollector

def create_market_list():
    print("▶ DART(금감원) 고유번호 API에서 마스터 상장사 리스트를 수집 중...")
    url = "https://opendart.fss.or.kr/api/corpCode.xml"
    res = requests.get(url, params={"crtfc_key": DART_API_KEY})
    
    if res.status_code != 200:
        print("❌ DART 마스터 파일 다운로드 실패")
        return []
        
    stocks = []
    try:
        with zipfile.ZipFile(io.BytesIO(res.content)) as z:
            with z.open('CORPCODE.xml') as f:
                tree = ET.parse(f)
                root = tree.getroot()
                for lst in root.findall('list'):
                    stock_code = lst.find('stock_code').text
                    corp_name = lst.find('corp_name').text
                    
                    if stock_code and stock_code.strip() and len(stock_code.strip()) == 6:
                        stocks.append({
                            "code": stock_code.strip(),
                            "name": corp_name.strip(),
                            "market": "코스피/코스닥"
                        })
    except Exception as e:
        print(f"❌ DART 파싱 오류: {e}")
        return []
        
    # 중복 방지 방어코드
    unique_stocks = {s['code']: s for s in stocks}
    final_list = list(unique_stocks.values())
    
    # ETF 등 상장지수펀드가 걸러지는 효과가 있어 순수 약 2500~2600개 기업만 추출됩니다.
    print(f"✅ 총 {len(final_list)} 개 종목 로딩 완료 (DART 기준 순수 기업)")
    return final_list

def main():
    stock_list = create_market_list()
    if not stock_list:
        return
        
    print("▶ KIS API 인증 중...")
    try:
        kis = KISCollector()
    except Exception as e:
        print(f"❌ KIS API 초기화 실패: {e}")
        return
        
    print(f"\n🚀 본격적인 전체 수집 시작 (총 {len(stock_list)} 종목)")
    print("Naver/DART를 배제하고 KIS API가 제공하는 '모든 시세/재무 데이터'를 수집합니다.")
    print("API Rate Limit(초당 15회) 기준으로 대략 15~20분 정도 소요될 수 있습니다.\n")
    
    all_data = {}
    total = len(stock_list)
    
    start_time = time.time()
    for idx, stock in enumerate(stock_list, 1):
        code = stock['code']
        name = stock['name']
        
        # 기본 필드 초기화
        stock_data = {
            "name": name,
            "market": stock['market'],
            "themes": [], # 헤비 크롤링 배제
            "price_today": {},
            "investor_today": {},
            "investor_5d": [],
            "daily_ohlcv": [],
            "program_5d": [],
            "short_data": {},
            "credit_data": [],
            "finance_ratio": {}
        }
        print(name)
        # 1. KIS 에러 무시 래퍼 (가독성 유지)
        def safe_fetch(func, *args, default=None):
            try:
                return func(*args)
            except Exception:
                return default if default is not None else {}
                
        # 2. 데이터 수집 (최대한 KIS 활용)
        stock_data['price_today'] = safe_fetch(kis.get_price, code)
        stock_data['daily_ohlcv'] = safe_fetch(kis.get_daily_price, code, "D")
        stock_data['program_5d'] = safe_fetch(kis.get_program_trade_5d, code, default=[])
        
        # KIS 공매도 데이터
        stock_data['short_data'] = safe_fetch(kis.get_short_sale, code)
        
        stock_data['credit_data'] = safe_fetch(kis.get_credit_balance, code, default=[])
        stock_data['finance_ratio'] = safe_fetch(kis.get_finance_ratio, code)
        
        # 수급 이력 다중 저장 (investor_today & investor_5d)
        inv_hist = safe_fetch(kis.get_investor_history, code, 5, default=[])
        stock_data['investor_5d'] = inv_hist
        if inv_hist and len(inv_hist) > 0:
            stock_data['investor_today'] = inv_hist[0]
            
        all_data[code] = stock_data
        
        # 매 종목마다 \r 을 이용해 한 줄에서 로딩바가 라이브로 갱신되도록 UI 개선
        elapsed_sec = time.time() - start_time
        elapsed_min = elapsed_sec / 60
        progress = (idx / total) * 100
        
        # 남은 시간 예상 (ETA 계산)
        eta_sec = (elapsed_sec / idx) * (total - idx)
        eta_min = eta_sec / 60
        
        sys.stdout.write(f"\r⏳ [{progress:>5.1f}%] {idx}/{total} 종목 수집 중... | 현재: {name:15s} | 누적 {elapsed_min:.1f}분 경과 (예상 남은시간: {eta_min:.1f}분)")
        sys.stdout.flush()

    # JSON 저장 (all_stocks.json 으로 명명하여 메인으로 사용)
    sys.stdout.write("\n") # 줄바꿈 한번 쳐주기
    today_str = datetime.now().strftime("%Y%m%d")
    out_dir = os.path.join(ROOT_DIR, "data")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"stock_data_kis_all_{today_str}.json")
    
    print(f"\n💾 수집 완료! 파일 저장 중... ({out_path})")
    
    with open(out_path, 'w', encoding='utf-8') as f:
        # 파일용량 최소화를 위해 들여쓰기 없음
        json.dump(all_data, f, ensure_ascii=False, separators=(',', ':'))
        
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"✅ 저장 성공! 파일 크기: {size_mb:.2f} MB")
    
if __name__ == "__main__":
    main()
