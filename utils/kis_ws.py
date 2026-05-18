import os
import json
import asyncio
import aiohttp
import websockets
from config.settings import KIS_APP_KEY, KIS_APP_SECRET, KIS_DOMAIN

# KIS WebSocket 도메인 설정 (환경변수에서 읽어오거나 명시된 포트 사용)
REAL_WS_URL = os.getenv("KIS_WSS", "ws://ops.koreainvestment.com:21000")
PAPER_WS_URL = os.getenv("KIS_PAPER_WSS", "ws://ops.koreainvestment.com:31000")

class KISWSManager:
    def __init__(self):
        self.approval_key = None
        self.ws = None
        self.subscriptions = set() # 종목 코드 집합
        self.callbacks = [] # 업데이트 수신 시 호출할 콜백 리스트
        self.running = False
        
    def get_ws_url(self):
        if "openapi.koreainvestment.com" in KIS_DOMAIN:
            return REAL_WS_URL
        return PAPER_WS_URL

    async def get_approval_key(self):
        """실시간 접속키 발급 (Approval Key)"""
        url = f"{KIS_DOMAIN}/oauth2/Approval"
        payload = {
            "grant_type": "client_credentials",
            "appkey": KIS_APP_KEY,
            "secretkey": KIS_APP_SECRET
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.approval_key = data.get("approval_key")
                    return self.approval_key
                else:
                    text = await resp.text()
                    print(f"❌ KIS Approval Key 발급 실패: {text}")
                    return None

    async def subscribe(self, stock_code: str):
        """특정 종목 실시간 체결가 구독"""
        if stock_code in self.subscriptions:
            return
        
        self.subscriptions.add(stock_code)
        if self.ws and self.ws.open:
            await self._send_subscribe(stock_code)

    async def _send_subscribe(self, stock_code: str):
        """구독 메시지 전송 (H0STCNT0: 국내주식 실시간 체결가)"""
        msg = {
            "header": {
                "approval_key": self.approval_key,
                "custtype": "P",
                "tr_type": "1",
                "content-type": "utf-8"
            },
            "body": {
                "input": {
                    "tr_id": "H0STCNT0",
                    "tr_key": stock_code
                }
            }
        }
        await self.ws.send(json.dumps(msg))
        print(f"📡 KIS WS 구독 요청: {stock_code}")

    async def start(self):
        """웹소켓 루프 시작"""
        if self.running: return
        self.running = True
        
        while self.running:
            try:
                if not self.approval_key:
                    await self.get_approval_key()
                
                if not self.approval_key:
                    print("⚠️ KIS Approval Key 발급 실패. 10초 후 재시도...")
                    await asyncio.sleep(10)
                    continue

                ws_url = self.get_ws_url()
                print(f"📡 KIS WebSocket 연결 시도: {ws_url}")
                async with websockets.connect(ws_url, handshake_timeout=30) as ws:
                    self.ws = ws
                    print("✅ KIS WebSocket 연결 성공")
                    
                    # 기존 구독 재신청
                    for code in self.subscriptions:
                        await self._send_subscribe(code)
                    
                    async for message in ws:
                        await self._handle_message(message)
                        
            except Exception as e:
                print(f"⚠️ KIS WebSocket 에러/끊김: {e}. 5초 후 재연결...")
                await asyncio.sleep(5)

    async def _handle_message(self, message):
        """수신 데이터 파싱 및 브로드캐스팅"""
        if message.startswith("0") or message.startswith("1"):
            # 데이터 수집 (포맷: 실시간 체결가 데이터는 파이프|로 구분됨)
            # 0|H0STCNT0|001|종목코드|...
            parts = message.split("|")
            if len(parts) >= 4:
                tr_id = parts[1]
                if tr_id == "H0STCNT0":
                    data_str = parts[3]
                    # 상세 파싱 (순서: 종목코드|체결시간|현재가|전일대비부호|...)
                    # 현재가는 보통 3번째 필드 (index 2 in parts[3] list?)
                    # KIS WS 데이터는 종목코드 이후부터 다시 파이프로 구분된 리스트임
                    sub_parts = data_str.split("^")
                    if len(sub_parts) >= 3:
                        stock_code = sub_parts[0]
                        current_price = int(sub_parts[2])
                        # 콜백 호출
                        for cb in self.callbacks:
                            await cb(stock_code, current_price)
        else:
            # 기타 메시지 (PONG 등)
            pass

    def add_callback(self, cb):
        self.callbacks.append(cb)

# 싱글톤 인스턴스
kis_ws_manager = KISWSManager()
