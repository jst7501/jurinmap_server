from datetime import datetime, date
from typing import Optional
from sqlmodel import SQLModel, Field


class Trade(SQLModel, table=True):
    """토스 매매내역서 1줄 = 1 Trade"""
    id: Optional[int] = Field(default=None, primary_key=True)
    traded_at: datetime = Field(index=True)            # 체결 일시
    symbol: str = Field(index=True)                    # 종목명
    ticker: Optional[str] = Field(default=None, index=True)  # 종목코드
    side: str                                          # "BUY" | "SELL"
    quantity: float                                    # 수량
    price: float                                       # 단가(체결가)
    amount: float                                      # 거래대금 (수량*단가)
    fee: float = 0.0                                   # 수수료
    tax: float = 0.0                                   # 세금
    currency: str = "KRW"
    source_file: Optional[str] = None                  # 원본 PDF 파일명
    created_at: datetime = Field(default_factory=datetime.utcnow)
