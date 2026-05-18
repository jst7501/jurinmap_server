import sys
import os
import asyncio
from datetime import datetime

# 프로젝트 루트 경로 추가 (telemsg.py 임포트용)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telemsg import analyze_with_ai, log_event_v2, init_db, send_push_alert

raw_text = """✅ 다날, 오픈AI·구글·MS와 함께 한다…'韓 기업 최초' AI 에이전트 재단 'AAIF' 입성 (상승 이유)

https://www.newsprime.co.kr/news/article/?no=729279"""

async def run_simulation():
    init_db()
    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    
    print("\n🔍 Gemini AI 뉴스 분석 시뮬레이션 중...\n")
    ai_data = await analyze_with_ai(raw_text)
    
    detected_stocks = []
    
    if ai_data:
        detected_stocks = ai_data.get('related_stocks', [])
        sentiment_emoji = {"호재": "📈", "악재": "📉", "중립": "➡️"}.get(ai_data.get('sentiment'), "❔")
        print(f"[{timestamp}] {sentiment_emoji} [AI 분석 결과]")
        print(f"  💎 헤드라인: {ai_data.get('headline')}")
        print(f"  📌 관련 종목: {', '.join(detected_stocks)}")
        print(f"  🏷️ 관련 테마: {ai_data.get('theme')}")
        print(f"  💬 분석 근거: {ai_data.get('reason')}")
    else:
        print("❌ AI 분석 실패")
        
    print("-" * 50)
    
    event_payload = {
        "timestamp": timestamp,
        "stocks": detected_stocks,
        "ai_analysis": ai_data,
        "content": raw_text
    }

    # DB 저장 처리
    print("💾 데이터베이스(단일 노드)에 데이터 강제 푸시 중...")
    log_event_v2(event_payload)

    # 서버 전송 처리
    print(f"🚀 서버로 푸시 알림 전송 중...")
    send_push_alert(event_payload)

if __name__ == "__main__":
    asyncio.run(run_simulation())
