import requests
import pandas as pd
import io

def test_krx():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "http://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201020302"
    })
    
    # 1. OTP 발급 (https로 시도)
    otp_url = "https://data.krx.co.kr/comm/fileDn/GenerateOTP/generate.cmd"
    bld = "db/MDC/STAT/standard/MDCSTAT02302"
    params = {
        "bld": bld,
        "name": "form",
        "mktId": "ALL",
        "invstTpCd": "ALL",
        "strtDd": "20260401",
        "endDd": "20260401",
        "share": "1",
        "money": "1",
        "csvPurp": "sel",
        "curcyTpCd": "THT"
    }
    
    print("OTP 요청 중...")
    r = session.get(otp_url, params=params)
    otp = r.text
    print(f"OTP: {otp}")
    
    if not otp or len(otp) > 100: # 에러 메시지인 경우
        print(f"OTP 에러: {r.text[:200]}")
        return

    # 2. 다운로드
    dn_url = "https://data.krx.co.kr/comm/fileDn/download_csv/download.cmd"
    print("다운로드 요청 중...")
    r = session.post(dn_url, data={"code": otp})
    print(f"Status: {r.status_code}")
    print(f"Content Type: {r.headers.get('Content-Type')}")
    
    if "text/csv" in r.headers.get('Content-Type', ''):
        df = pd.read_csv(io.BytesIO(r.content), encoding='cp949')
        print(df.head())
    else:
        print("CSV 데이터가 아닙니다.")
        if r.text:
             print(r.text[:500])

if __name__ == "__main__":
    test_krx()
