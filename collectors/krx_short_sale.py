import requests
from datetime import datetime, timedelta

class KRXShortSaleCollector:
    def __init__(self):
        self.url = "http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
        self.headers = {
            "Referer": "http://data.krx.co.kr/contents/MDC/MDI/mdc0603030201.cmd",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/x-www-form-urlencoded"
        }

    def fetch_all_balances(self):
        """
        KRX 정보데이터시스템에서 가장 최근 유효한 공매도 잔고 데이터를 수집합니다.
        공매도 잔고는 규정상 T-2 일 부터 공개되므로 최근 2~5일 사이의 데이터를 재시도하며 조회합니다.
        """
        print("  > KRX 통신: 전 종목 공매도 잔고 현황 로딩 중... ", end="")
        
        for i in range(2, 7):
            target_date = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
            data = {
                "bld": "dbms/MDC/STAT/srt/MDCSTAT30101",
                "mktId": "ALL",
                "trdDd": target_date
            }
            
            try:
                res = requests.post(self.url, headers=self.headers, data=data, timeout=10)
                if res.status_code == 200:
                    out = res.json()
                    outblock = out.get("OutBlock_1", [])
                    if len(outblock) > 0:
                        # 데이터가 존재하는 날짜 발견
                        result_map = {}
                        for row in outblock:
                            code = row.get("ISU_SRT_CD", "")
                            qty = str(row.get("CVSRT_BAL_QTY", "0")).replace(",", "")
                            amt = str(row.get("CVSRT_BAL_AMT", "0")).replace(",", "")
                            
                            if code:
                                result_map[code] = {
                                    "short_balance_qty": int(qty) if qty.isdigit() else 0,
                                    "short_balance_amt": int(amt) if amt.isdigit() else 0,
                                    "date": target_date
                                }
                        print(f"완료 (기준일: {target_date}, {len(result_map)}개 종목)")
                        return result_map
            except Exception as e:
                pass
                
        print("실패 (최근 5일간 데이터 없음)")
        return {}
