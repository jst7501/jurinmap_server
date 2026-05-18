from pykrx import stock
from datetime import datetime, timedelta

def test_short():
    code = "046970" # 우리로
    today = datetime.now().strftime("%Y%m%d")
    past = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
    
    print(f"Testing for {code} from {past} to {today}")
    
    try:
        df1 = stock.get_shorting_status_by_date(past, today, code)
        print("Shorting Status (Lending):")
        print(df1.tail())
    except Exception as e:
        print(f"Error Status: {e}")
        
    try:
        df2 = stock.get_shorting_balance_by_date(past, today, code)
        print("\nShorting Balance:")
        print(df2.tail())
    except Exception as e:
        print(f"Error Balance: {e}")

if __name__ == "__main__":
    test_short()
