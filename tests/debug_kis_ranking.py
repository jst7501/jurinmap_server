import requests
import json
from collectors.kis_api import KISCollector

def debug_ranking_api():
    kis = KISCollector()
    path = "/uapi/domestic-stock/v1/ranking/trade-value"
    
    # KIS 랭킹 API는 파라미터를 쿼리 스트링으로 보내야 하며, 대문자여야 함
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_COND_SCR_DIV_CODE": "20171",
        "FID_INPUT_ISCD": "0001",
        "FID_DIV_CLS_CODE": "0",
        "FID_BLNG_CLS_CODE": "0",
        "FID_TRGT_CLS_CODE": "0",
        "FID_TRGT_EXCL_CLS_CODE": "0",
        "FID_VOL_CNT": "100",
        "FID_INPUT_PRICE_1": "",
        "FID_INPUT_PRICE_2": ""
    }
    
    headers = kis.headers.copy()
    headers["tr_id"] = "FHPST01710000"
    url = f"{kis.base_url}{path}"
    
    print(f"URL: {url}")
    print(f"Headers: { {k:v for k,v in headers.items() if k != 'appsecret'} }")
    
    response = requests.get(url, headers=headers, params=params)
    print(f"Status Code: {response.status_code}")
    try:
        print("Response JSON:")
        print(json.dumps(response.json(), indent=2, ensure_ascii=False))
    except:
        print(f"Raw Response: {response.text}")

if __name__ == "__main__":
    import os
    import sys
    sys.path.append(os.getcwd())
    debug_ranking_api()
