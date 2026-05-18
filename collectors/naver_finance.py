import requests
from bs4 import BeautifulSoup
import pandas as pd

class NaverCollector:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
    def get_financial_summary(self, stock_code):
        """
        네이버 금융 종목 정보 주요 지표 스크래핑
        시가총액, 유동주식비율 등 추출
        """
        url = f"https://finance.naver.com/item/main.naver?code={stock_code}"
        res = requests.get(url, headers=self.headers)
        
        data = {}
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            
            # 시가총액 추출 시도
            try:
                # 시가총액 (억 단위 문자열)
                market_cap_em = soup.select_one("#_market_sum")
                if market_cap_em:
                    data['market_cap'] = market_cap_em.get_text(strip=True) + "억"
            except:
                data['market_cap'] = None

            # 유동주식비율 및 상장주식수 추출 (테이블에서 직접)
            try:
                import io
                all_tables = pd.read_html(io.StringIO(res.text))
                
                for table in all_tables:
                    if '상장주식수' in table.values:
                        row_idx = table[table[0].str.contains('상장주식수', na=False)].index[0]
                        val = str(table.iloc[row_idx, 1]).split()[0].replace(',', '')
                        data['listed_shares_count'] = int(val)
                    if '유동주식비율' in table.values:
                        row_idx = table[table[0].str.contains('유동주식비율', na=False)].index[0]
                        val = str(table.iloc[row_idx, 1]).replace(',', '').replace('%', '')
                        data['floating_ratio_pct'] = float(val)
            except:
                pass
                
            try:
                import io
                # 기업실적분석 테이블 (보통 4번째)
                tables = pd.read_html(io.StringIO(res.text))
                if len(tables) >= 4:
                    financial_table = tables[3] 
                    financial_table = financial_table.where(pd.notnull(financial_table), None)
                    financial_table.columns = ['_'.join(map(str, col)).strip() for col in financial_table.columns.values]
                    data['financial_table'] = financial_table.to_dict(orient='records')
            except Exception as e:
                data['financial_table'] = {"error": str(e)}
                
        return data
    def get_short_sell_data(self, stock_code):
        """
        네이버 증권 공매도 현황 수집
        """
        url = f"https://finance.naver.com/item/short_term.naver?code={stock_code}"
        res = requests.get(url, headers=self.headers)
        
        data = {
            "short_selling_volume_ratio": 0,
            "short_vol": 0,
            "short_amt": 0,
            "margin_loan_rate": 0
        }
        
        if res.status_code == 200:
            try:
                import io
                tables = pd.read_html(io.StringIO(res.text))
                if len(tables) >= 1:
                    # 첫 번째 테이블이 일별 공매도 현황
                    df = tables[0]
                    # 컬럼명이 멀티 인덱스일 수 있음, 정리 필요
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(-1)
                    
                    # 최신 행(보통 첫 줄) 추출
                    latest = df.iloc[0]
                    data["short_vol"] = int(str(latest['공매도량']).replace(',', '').split('.')[0])
                    data["short_selling_volume_ratio"] = float(str(latest['비중']).replace(',', '').replace('%', ''))
                    data["short_amt"] = int(str(latest['거래대금']).replace(',', '').split('.')[0])
            except Exception as e:
                print(f"  [Naver Short Error] {e}")

        # 신용잔고율 추가 (main.naver 페이지에서 추출)
        try:
            main_url = f"https://finance.naver.com/item/main.naver?code={stock_code}"
            main_res = requests.get(main_url, headers=self.headers)
            soup = BeautifulSoup(main_res.text, "html.parser")
            loan_rate_tag = soup.select_one(".tab_con1 .c_up") # 단순 예시, 실제 위치 확인 필요
            # pd.read_html로 재시도 (더 정확함)
            main_tables = pd.read_html(io.StringIO(main_res.text))
            for mt in main_tables:
                if '신용비율' in mt.values:
                     row = mt[mt[0].str.contains('신용비율', na=False)]
                     if not row.empty:
                         val = str(row.iloc[0, 1]).replace(',', '').replace('%', '')
                         data["margin_loan_rate"] = float(val)
        except:
            pass

        return data
