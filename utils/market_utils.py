from datetime import datetime, time
import pytz

def get_us_market_status():
    """
    미국 시장 현재 상태 반환 (PRE, REG, AFTER, CLOSED)
    """
    est = pytz.timezone('US/Eastern')
    now_est = datetime.now(est)
    
    # 주말 체크
    if now_est.weekday() >= 5:
        return "CLOSED"
    
    cur_time = now_est.time()
    
    # 프리마켓: 04:00 ~ 09:30
    if time(4, 0) <= cur_time < time(9, 30):
        return "PRE"
    # 정규장: 09:30 ~ 16:00
    elif time(9, 30) <= cur_time < time(16, 0):
        return "REG"
    # 애프터장: 16:00 ~ 20:00
    elif time(16, 0) <= cur_time < time(20, 0):
        return "AFTER"
    else:
        return "CLOSED"

def get_market_opening_seconds():
    """다음 장 시작 또는 현재 장 종료까지 남은 시간 (간략)"""
    # ... (필요 시 구현)
    pass
