from pykrx import stock
from datetime import datetime
import pandas as pd

try:
    # 가장 최근 가장 가까운 영업일을 가져와보자
    print(stock.get_business_days_of_month(datetime.now().year, datetime.now().month))
except Exception as e:
    print(e)
