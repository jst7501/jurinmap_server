from collectors.kis_api import KISCollector
import json

def test_kis_data():
    kis = KISCollector()
    code = "046970" # 우리로
    
    print(f"Testing KIS Daily Price (FHKST01010400) for {code}...")
    daily_res = kis.get_daily_price(code)
    if 'output' in daily_res and len(daily_res['output']) > 0:
        print(f"  Latest Day (output[0]): {daily_res['output'][0]['stck_bsop_date']} clpr={daily_res['output'][0]['stck_clpr']}")
    else:
        print(f"  Daily Price failed: {daily_res.get('msg1') or daily_res}")

    paths = [
        "/uapi/domestic-stock/v1/quotations/inquire-daily-short-sell",
        "/uapi/domestic-stock/v1/quotations/inquire-daily-short-selling",
        "/uapi/domestic-stock/v1/quotations/inquire-short-sell-daily",
        "/uapi/domestic-stock/v2/quotations/inquire-daily-short-sell",
    ]
    
    print(f"\n--- Testing Path Variations for FHPST01010100 ---")
    for p in paths:
        print(f"Testing {p}...")
        res = kis._get(p, {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0"
        }, "FHPST01010100")
        if 'error' not in res:
            print(f"  SUCCESS on {p}!")
            print(f"  Data extract: {res.get('msg1')}")
            break
        else:
            print(f"  Failed: {res.get('error')} ({res.get('status')})")

    print(f"\nTesting KIS Margin/Lending for {code}...")
    margin_res = kis.get_margin_lending_data(code)
    if 'output2' in margin_res and len(margin_res['output2']) > 0:
        print(f"  Latest Day (output2[0]): {margin_res['output2'][0]}")
    else:
        print(f"  Output2 empty or missing: {margin_res.get('msg1')}")

if __name__ == "__main__":
    test_kis_data()
