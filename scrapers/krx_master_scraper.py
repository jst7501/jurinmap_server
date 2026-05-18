import requests
import pandas as pd
import io
import datetime
import time

class KRXMasterScraper:
    def __init__(self):
        self.generate_url = "https://data.krx.co.kr/comm/fileDn/GenerateOTP/generate.cmd"
        self.download_url = "https://data.krx.co.kr/comm/fileDn/download_csv/download.cmd"
        self.api_url = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://data.krx.co.kr",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty"
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        
        # [중요] 세션 초기화: 메인 페이지를 먼저 방문하여 기본 쿠키(JSESSIONID 등)를 확보합니다.
        try:
            self.session.get("https://data.krx.co.kr/main/main.cmd", timeout=10)
        except Exception as e:
            print(f"  [!] KRX 세션 초기화 실패: {e}")

    def _get_otp(self, bld, params, menu_id):
        """OTP를 생성하여 다운로드 키를 반환합니다."""
        referer = f"https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId={menu_id}"
        self.session.headers.update({"Referer": referer})
        
        # 1. 먼저 해당 메뉴 페이지에 대기 세션 생성
        self.session.get(referer)
        
        # 2. OTP 생성 요청 (POST 방식)
        data = {"bld": bld, "name": "form", **params}
        response = self.session.post(self.generate_url, data=data)
        
        if response.status_code == 200:
            otp = response.text.strip()
            print(f"DEBUG: OTP acquired for {menu_id}")
            return otp
        return None

    def _download_csv(self, otp):
        """OTP를 사용하여 실시간 CSV 데이터를 다운로드하고 Pandas DataFrame으로 반환합니다."""
        if not otp:
            return None
        
        # 3. CSV 다운로드 요청 (POST 방식)
        response = self.session.post(self.download_url, data={"code": otp})
        if response.status_code == 200:
            if "text/csv" in response.headers.get('Content-Type', ''):
                # KRX CSV는 cp949 인코딩을 사용함
                df = pd.read_csv(io.BytesIO(response.content), encoding='cp949')
                return df
            else:
                print(f"DEBUG: Failed to get CSV content. Check OTP or Parameters.")
        return None

    def fetch_investor_activity(self, start_date, end_date, mkt_id='ALL'):
        """투자자별 거래실적 (개별종목 - 누적) 데이터를 가져옵니다."""
        # MDC0201020302
        bld = "db/MDC/STAT/standard/MDCSTAT02302"
        params = {
            "mktId": mkt_id,
            "invstTpCd": "ALL",
            "strtDd": start_date,
            "endDd": end_date,
            "share": "1",
            "money": "1",
            "csvPurp": "sel",
            "curcyTpCd": "THT"
        }
        otp = self._get_otp(bld, params, "MDC0201020302")
        return self._download_csv(otp)

    def fetch_short_selling(self, date, mkt_id='ALL'):
        """공매도 현황 데이터를 가져옵니다."""
        # MDC0201030101
        bld = "db/MDC/STAT/standard/MDCSTAT01501"
        params = {
            "mktId": mkt_id,
            "trdDd": date,
            "share": "1",
            "money": "1",
            "csvPurp": "sel"
        }
        otp = self._get_otp(bld, params, "MDC0201030101")
        return self._download_csv(otp)

    def fetch_loan_balance(self, date, mkt_id='ALL'):
        """대차잔고 현황 데이터를 가져옵니다."""
        # MDC0201030201
        bld = "db/MDC/STAT/standard/MDCSTAT01601"
        params = {
            "mktId": mkt_id,
            "trdDd": date,
            "csvPurp": "sel"
        }
        otp = self._get_otp(bld, params, "MDC0201030201")
        return self._download_csv(otp)

    def fetch_short_selling_by_item(self, isu_cd, strt_dd, end_dd):
        """종목별 공매도 거래 현황(JSON)을 가져오는 '진짜' 뚫어내기 버전 (MDCSTAT300)"""
        bld = "db/MDC/STAT/srt/MDCSTAT30001"
        
        # 사용자님 로그와 동일하게 단축코드(005930) 그대로 사용
        params = {
            "isuCd": isu_cd, 
            "strtDd": strt_dd,
            "endDd": end_dd,
            "share": "1",
            "money": "1",
            "searchType": "1" 
        }
        
        # 1. OTP 생성 (Referer를 로더 페이지로 설정하여 서버를 속임)
        # 로더 URL: https://data.krx.co.kr/comm/srt/srtLoader/index.cmd?screenId=MDCSTAT300&isuCd=005930
        loader_url = f"https://data.krx.co.kr/comm/srt/srtLoader/index.cmd?screenId=MDCSTAT300&isuCd={isu_cd}"
        
        print(f"DEBUG: OTP 발급 시도중... (bld={bld})")
        otp = self._get_otp_advanced(bld, params, loader_url)
        if not otp:
            return None
            
        # 2. JSON 데이터 요청 (사용자 제공 헤더 및 바디와 일치화)
        data = {
            "bld": bld,
            "otp": otp,
            **params
        }
        
        headers = self.headers.copy()
        headers["Referer"] = loader_url
        
        try:
            response = self.session.post(self.api_url, data=data, headers=headers, timeout=10)
            if response.status_code == 200:
                try:
                    json_data = response.json()
                    if 'output' in json_data and json_data['output']:
                        return pd.DataFrame(json_data['output'])
                    else:
                        print(f"DEBUG: API 응답 성공하나 데이터 없음. JSON: {json_data}")
                except Exception:
                    print(f"DEBUG: JSON 파싱 실패. 응답: {response.text[:200]}")
            else:
                print(f"DEBUG: KRX API 서버 응답 오류({response.status_code}) 내용: {response.text[:100]}")
            return None
        except Exception as e:
            print(f"  [!] KRX API 오류: {e}")
            return None

    def _get_otp_advanced(self, bld, params, referer):
        """커스텀 Referer를 지원하는 고도화된 OTP 생성 메서드"""
        # 세션 초기화 및 로더 방문
        self.session.headers.update({"Referer": "https://data.krx.co.kr/main/main.cmd"})
        self.session.get(referer, timeout=10)
        
        # OTP 생성 요청
        self.session.headers.update({"Referer": referer})
        data = {"bld": bld, "name": "form", **params}
        response = self.session.post(self.generate_url, data=data, timeout=10)
        
        if response.status_code == 200:
            return response.text.strip()
        print(f"DEBUG: OTP 발급 오류({response.status_code}): {response.text[:100]}")
        return None

if __name__ == "__main__":
    scraper = KRXMasterScraper()
    today = datetime.datetime.now().strftime("%Y%m%d")
    # 어제 날짜로 테스트 (장이 열리지 않았을 때 대비)
    yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y%m%d")
    
    # 최근 영업일로 테스트 (2026-04-03 금요일)
    test_date = "20260403"
    print(f"[{test_date}] KRX 데이터 수집 및 JSON API 테스트 중...")
    
    # 4. 종목별 공매도 (MDCSTAT300 - JSON API)
    short_by_item_df = scraper.fetch_short_selling_by_item("005930", test_date, test_date)
    if short_by_item_df is not None:
        print(f"삼성전자(005930) 공매도 JSON API 수집 완료: {len(short_by_item_df)} 건")
        print(short_by_item_df.head(5))
