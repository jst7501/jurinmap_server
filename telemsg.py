# Telegram App 자격증명은 .env (TELEGRAM_API_ID / TELEGRAM_API_HASH) 로 이전됨 (2026-05-18)
import os
import re
import json
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from telethon import TelegramClient, events
import psycopg2
from psycopg2.extras import RealDictCursor
import requests
from google import genai
import sys
import hashlib

# Windows 콘솔 유니코드 출력 호환성 설정
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass
from config.stock_list import TARGET_STOCKS
from collectors.kis_api import KISCollector

STOCK_NAME_TO_CODE = {str(name).strip(): str(code).strip() for code, name in TARGET_STOCKS.items()}

# .env 파일 로드
load_dotenv()

# Gemini API 설정 — 2026-05-18: 하드코딩 키 제거, .env 의 GEMINI_API_KEY 사용
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    client_genai = genai.Client(api_key=GEMINI_API_KEY)
else:
    client_genai = None
    print("⚠️  GEMINI_API_KEY가 .env에 없습니다. AI 분석 기능이 비활성화됩니다.")

# Telegram 자격증명 — .env 의 TELEGRAM_API_ID / TELEGRAM_API_HASH
api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
api_hash = os.getenv("TELEGRAM_API_HASH", "")

# 메시지를 가져올 대상 채널의 유저네임 (예: @stock_news_channel) 또는 채널 ID
target_channel = '@stock0'
PUSH_NEWS_ENDPOINT = os.getenv("PUSH_NEWS_ENDPOINT", "http://127.0.0.1:8000/api/push/news")
TELEMSG_PUSH_TOKEN = os.getenv("TELEMSG_PUSH_TOKEN", "")

# 스팸/필터링 키워드
SPAM_KEYWORDS = [
    "채널 공유 요청",
    "공유해주시면 감사",
    "지인분들께 같이 공유",
    "상식채널",
    "시간외 단일가",
    "키워드뉴스 Pro",
"지표 공유",
    "주식채널",
    "시간단타",
    "유료채널",
    "수익인증",
    "트레이더",
    "일정매매",
    "스윙매매",
    "초단타",
    "문의 @",
    "상담 @",
    "입금 계좌",
    "카카오톡 오픈채팅",
    "무료 체험",
    "VIP 회원",
    # 개인 의견/잡담 필터
    "때문에 올랐",
    "때문에 내렸",
    "보군요",
    "것 같아요",
    "것 같습니다",
    "인 것 같",
    "ㅋㅋ",
    "ㅎㅎ",
    "ㅠㅠ",
    "감사합니다",
    "좋은 하루",
    "오늘도 화이팅",
    "공유 감사",
]


# AI 분석 프롬프트
GEMINI_ANALYSIS_PROMPT = """
너는 한국 주식 시장의 실시간 뉴스와 정보지(찌라시)를 분석하는 금융 데이터 전문 AI야.
사용자가 입력한 텍스트를 분석하여 반드시 아래 지정된 JSON 스키마 형태로만 응답해야 해.
JSON 외에 어떠한 인사이트나 부연 설명도 출력하지 마.

[현재 시점 컨텍스트 — 2026년 5월 기준 — 반드시 준수]
- 대한민국 현직 대통령: **이재명** (2025년 6월 취임, 더불어민주당)
- 윤석열 전 대통령은 2025년 4월 4일 헌법재판소 탄핵 인용으로 파면됨
- 뉴스 원문에서 "李대통령", "이 대통령", "대통령"으로만 표기된 경우 **반드시 "이재명 대통령"으로 해석**
- 절대로 "윤석열 대통령"이라고 표기하지 마. 사전 학습 시점의 정치 상황에 의존하지 말 것.
- 한국 정부 = "이재명 정부" (윤석열 정부는 과거 시점에만 사용 가능)

[분석 및 추출 규칙]
1. headline: 텍스트의 핵심을 요약하여 직관적인 1줄 헤드라인으로 추출해.
2. source_type: "공식뉴스"(언론사/기자 명시) 또는 "미확인보도"(출처 불분명/찌라시/선동성)로 분류해.
3. sentiment: 해당 정보가 관련 종목에 미칠 단기적 영향력을 "호재", "악재", "중립" 중 하나로 평가해.
4. sentiment_score: 1부터 10까지 정수. 매우 보수적으로 책정해.
      - 1-2: 영향 미미 / 3-5: 일반적 호재/악재 / 6-8: 강력한 모멘텀 / 9-10: 메가급 뉴스

5. related_stocks: 텍스트 직접 언급 종목 및 테마 수혜성 종목 추출 (최대 6개) 반드시 '대한민국 거래소(KRX)'에 상장된 종목명만 추출.
6. theme: 뉴스와 연관된 핵심 테마명.
7. reason: 점수 산정 근거를 데이터 기반으로 설명해. **아래 안전 수칙을 반드시 준수해.**
원자재(LNG, 석유 등) 가격 하락 소식은 수입/유통사(SK가스 등)에게 무조건적인 호재가 아님을 유의하라.

가격 하락 시 '재고 평가 손실'이나 '판가 하락에 따른 마진 축소' 가능성이 있다면 "악재" 혹은 **"중립"**으로 분석하라.

공급 과잉 이슈는 유통사에게 판매 단가 하락 압박으로 작용하므로 데이터 기반으로 엄격하게 점수를 산정하라.

[금지 사항 및 안전 수칙]
- 절대 "매수", "매도", "추천", "필승", "지금 사세요" 등 직접적인 투자 조언이나 지시를 내리지 마.
- 특정 개인(예: "승태님")을 지칭하는 개인화된 표현을 절대 사용하지 마.
- 대신 "데이터 기반 수혜 가능성", "통계적 하락 변동성 확률", "역사적 사례 기반 영향" 등 객관적이고 데이터 중심적인 표현을 사용해.
- 모든 답변은 데이터 제공 도구로서의 중립성을 유지해야 해.
- 해외 주식(NVDA, TSLA, AAPL 등) 및 해외 증시 데이터는 분석 대상에서 제외하며 언급도 하지 마.

[출력 JSON 형식]
{
  "headline": "문자열",
  "source_type": "공식뉴스" | "미확인보도",
  "sentiment": "호재" | "악재" | "중립",
  "sentiment_score": 1~10,
  "related_stocks": ["종목명1", "종목명2"],
  "theme": "테마명",
  "reason": "데이터 기반 분석 근거"
}
"""

async def analyze_with_ai(text):
    """Gemini를 사용해 뉴스 분석 및 JSON 반환"""
    if not client_genai:
        return None
    
    try:
        response = await asyncio.to_thread(
            client_genai.models.generate_content,
            model='gemini-2.5-flash-lite',
            contents=f"{GEMINI_ANALYSIS_PROMPT}\n\n입력: {text}"
        )
        # JSON 문자열만 추출 (백틱 제거 등)
        clean_json = response.text.strip().replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json)
        return data
    except Exception as e:
        print(f"  [AI Error] {e}")
        return None

# 세션 파일 이름을 다시 지정 (한 번 인증 후 유지되도록)
client = TelegramClient('news_scraper_session', api_id, api_hash)

def get_pg_conn():
    """PostgreSQL 연결 생성"""
    return psycopg2.connect(
        host=os.getenv("PGHOST", "127.0.0.1"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "postgres"),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", "7410"),
        sslmode=os.getenv("PGSSLMODE", "disable")
    )


def _normalize_stock_names(raw):
    if isinstance(raw, str):
        items = [s.strip() for s in raw.split(",") if s.strip()]
    elif isinstance(raw, list):
        items = [str(s).strip() for s in raw if str(s).strip()]
    else:
        items = []
    seen = set()
    out = []
    for name in items:
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _resolve_stock_code(cursor, stock_name: str) -> str:
    code = STOCK_NAME_TO_CODE.get(stock_name)
    if code:
        return code
    try:
        cursor.execute("SELECT code FROM stocks WHERE name = %s LIMIT 1", (stock_name,))
        row = cursor.fetchone()
        if row and row.get("code"):
            return str(row["code"]).strip()
    except Exception:
        pass
    return ""


def _filter_stocks_with_db_price(stock_names):
    """stocks 테이블에서 code 해석 가능한 종목만 통과 (price_today 없어도 OK)"""
    candidates = _normalize_stock_names(stock_names)
    if not candidates:
        return []

    filtered = []
    conn = None
    try:
        conn = get_pg_conn()
        c = conn.cursor(cursor_factory=RealDictCursor)
        for stock_name in candidates:
            code = _resolve_stock_code(c, stock_name)
            if not code:
                continue  # 코드 자체를 못 찾으면 제외 (AI 오탐)
            filtered.append(stock_name)
    except Exception as e:
        print(f"  [Stock Filter Error] {e}")
        return []
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass

    return filtered

def init_db():
    try:
        conn = get_pg_conn()
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS news_events (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMP,
                headline TEXT,
                source_type TEXT,
                sentiment TEXT,
                sentiment_score INTEGER,
                related_stocks TEXT,
                theme TEXT,
                reason TEXT,
                raw_content TEXT,
                publish_prices_json TEXT,
                source_url TEXT
            )
        ''')
        c.execute("ALTER TABLE news_events ADD COLUMN IF NOT EXISTS source_url TEXT")
        c.execute("ALTER TABLE news_events ADD COLUMN IF NOT EXISTS sentiment_score INTEGER")
        c.execute("ALTER TABLE news_events ADD COLUMN IF NOT EXISTS publish_prices_json TEXT")
        c.execute("ALTER TABLE news_events ADD COLUMN IF NOT EXISTS content_hash TEXT UNIQUE")
        
        try:
            c.execute("CREATE SEQUENCE IF NOT EXISTS news_events_id_seq")
            c.execute("ALTER TABLE news_events ALTER COLUMN id SET DEFAULT nextval('news_events_id_seq')")
            c.execute("""
                SELECT setval('news_events_id_seq', 
                              (SELECT COALESCE(MAX(id), 0) + 1 FROM news_events),
                              false)
            """)
        except Exception:
            pass
            
        conn.commit()
        c.close()
        conn.close()
        print("✅ [PostgreSQL] news_events 테이블 확인 완료")
    except Exception as e:
        print(f"❌ [PostgreSQL] DB 초기화 실패: {e}")

def get_content_hash(text: str) -> str:
    """텍스트의 SHA256 해시 반환"""
    return hashlib.sha256(str(text).strip().encode('utf-8')).hexdigest()

def is_duplicate_news(content_hash: str) -> bool:
    """이미 처리된 뉴스인지 확인"""
    try:
        conn = get_pg_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id FROM news_events WHERE content_hash = %s LIMIT 1", (content_hash,))
        row = cur.fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False

def fetch_stock_prices(detected_stocks):
    """종목별 현재가 및 코드 정보 수집"""
    publish_prices: dict = {}
    if not detected_stocks:
        return publish_prices

    conn = None
    try:
        conn = get_pg_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        collector = KISCollector()

        for stock_name in detected_stocks:
            stock_code = next((code for code, n in TARGET_STOCKS.items() if n == stock_name), None)
            if not stock_code:
                try:
                    cur.execute("SELECT code FROM stocks WHERE name = %s LIMIT 1", (stock_name,))
                    row = cur.fetchone()
                    if row: stock_code = row["code"]
                except Exception: pass
            if not stock_code:
                try:
                    cur.execute("SELECT code FROM kr_etf_master WHERE name = %s LIMIT 1", (stock_name,))
                    row = cur.fetchone()
                    if row: stock_code = row["code"]
                except Exception: pass
            
            if not stock_code: continue

            curr_p, change_pct = None, None
            try:
                pd_data = collector.get_price(stock_code)
                curr_p     = pd_data.get("current_price")
                change_pct = pd_data.get("change_pct")
            except Exception: pass

            if not curr_p:
                try:
                    cur.execute("SELECT current_price, change_pct FROM price_today WHERE code=%s", (stock_code,))
                    row_p = cur.fetchone()
                    if row_p:
                        curr_p = row_p["current_price"]
                        change_pct = row_p["change_pct"]
                except Exception: pass

            publish_prices[stock_name] = {
                "code": stock_code,
                "price": curr_p,
                "change_pct": change_pct
            }
        
    except Exception as e:
        print(f"  [Price Fetch Error] {e}")
    finally:
        if conn: conn.close()
    
    return publish_prices


def _build_push_payload_v2(event_data):
    ai_data = event_data.get("ai_analysis") or {}
    raw_content = str(event_data.get("content") or "").strip()
    first_line = ""
    for line in raw_content.splitlines():
        t = str(line or "").strip()
        if t:
            first_line = t
            break

    title = str(event_data.get("headline") or ai_data.get("headline") or first_line or "실시간 뉴스 알림").strip()

    # body: 연관종목 있으면 종목 리스트, 없으면 AI 요약 or 원문
    stocks = ai_data.get("related_stocks") or []
    sentiment = ai_data.get("sentiment", "")
    score = ai_data.get("sentiment_score", "")
    theme = ai_data.get("theme", "")
    
    # 가격 정보가 있으면 body에 추가
    prices_list = []
    prices_data = event_data.get("publish_prices") or {}
    for sname in stocks[:2]:
        pinfo = prices_data.get(sname)
        if pinfo and pinfo.get("price"):
            c_pct = pinfo.get("change_pct")
            pct_str = f"({c_pct:+.1f}%)" if c_pct is not None else ""
            prices_list.append(f"{sname} {int(pinfo['price']):,}{pct_str}")

    if stocks:
        emoji = {"호재": "📈", "악재": "📉", "중립": "➡️"}.get(sentiment, "")
        parts = []
        if sentiment and score:
            parts.append(f"{emoji} {sentiment} {score}/10")
        if theme:
            parts.append(f"#{theme}")
        
        if prices_list:
            parts.append(" | ".join(prices_list))
        else:
            parts.append(f"연관: {', '.join(stocks[:4])}")
            
        body = " · ".join(parts)
    else:
        reason = str(ai_data.get("reason") or "").strip()
        body = reason[:200] if reason else " ".join(raw_content.split()).strip()[:200] or title

    return {
        "title": title[:120],
        "body": body[:240],
        "url": "/#/news",
        "tag": "telemsg-news",
        "icon": "/favicon.svg",
        "meta": {
            "timestamp": event_data.get("timestamp"),
            "sentiment": sentiment,
            "score": score,
            "stocks": stocks[:4],
            "prices": prices_data
        },
    }

def send_push_alert(event_data, timeout=10):
    try:
        payload = _build_push_payload_v2(event_data)
        headers = {"Content-Type": "application/json"}
        if TELEMSG_PUSH_TOKEN:
            headers["x-telemsg-token"] = TELEMSG_PUSH_TOKEN

        response = requests.post(PUSH_NEWS_ENDPOINT, json=payload, headers=headers, timeout=timeout)
        if not response.ok:
            print(f"  [Push Error] {response.status_code} {response.text[:180]}")
    except Exception as e:
        print(f"  [Push Error] {e}")

def log_event_v2(event_data):
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    log_path = os.path.join(log_dir, "telegram_events.jsonl")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event_data, ensure_ascii=False) + "\n")

    try:
        conn = get_pg_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        timestamp = event_data.get("timestamp")
        raw_content = str(event_data.get("content") or "").strip()
        ai_data = event_data.get("ai_analysis") or {}
        content_hash = event_data.get("content_hash") or get_content_hash(raw_content)
        
        first_line = next((l.strip() for l in raw_content.splitlines() if l.strip()), "")

        headline     = str(event_data.get("headline") or ai_data.get("headline") or first_line or "").strip()
        source_type  = str(ai_data.get("source_type") or "").strip()
        sentiment    = str(ai_data.get("sentiment") or "").strip()
        sentiment_score = ai_data.get("sentiment_score")
        theme        = str(ai_data.get("theme") or "").strip()
        reason       = str(ai_data.get("reason") or "").strip()

        detected_stocks = _normalize_stock_names(ai_data.get("related_stocks", []))
        related_stocks = ", ".join(detected_stocks)

        url_match  = re.search(r'https?://[^\s\)\]\>\"\']+', raw_content)
        source_url = url_match.group(0) if url_match else ""

        # 이미 수집된 가격 정보가 있으면 사용, 없으면 새로 수집
        publish_prices = event_data.get("publish_prices") or fetch_stock_prices(detected_stocks)
        publish_prices_json = json.dumps(publish_prices, ensure_ascii=False)

        cur.execute("""
            INSERT INTO news_events
                (timestamp, headline, source_type, sentiment, sentiment_score,
                 related_stocks, theme, reason, raw_content, publish_prices_json, source_url, content_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (content_hash) DO NOTHING
            """, (timestamp, headline, source_type, sentiment, sentiment_score,
                  related_stocks, theme, reason, raw_content, publish_prices_json, source_url, content_hash))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  [DB Log Error] {e}")


async def new_message_handler(event):
    raw_text = getattr(event, 'raw_text', '')
    if not raw_text and hasattr(event, 'message'):
        msg_obj = event.message
        raw_text = msg_obj if isinstance(msg_obj, str) else getattr(msg_obj, 'message', '')
            
    if not raw_text or any(k in raw_text for k in SPAM_KEYWORDS):
        return

    # 중복 뉴스 체크
    content_hash = get_content_hash(raw_text)
    if await asyncio.to_thread(is_duplicate_news, content_hash):
        return

    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

    try:
        print(f"\n[{timestamp}] 🔍 데이터 분석 중...")
        ai_data = await analyze_with_ai(raw_text)
        
        publish_prices = {}
        detected_stocks = []
        if ai_data:
            detected_stocks = _normalize_stock_names(ai_data.get('related_stocks', []))
            detected_stocks = _filter_stocks_with_db_price(detected_stocks)
            ai_data['related_stocks'] = detected_stocks
            
            # 가격 정보 즉시 수집 (푸시에 포함하기 위함)
            publish_prices = await asyncio.to_thread(fetch_stock_prices, detected_stocks)
            
            sentiment_emoji = {"호재": "📈", "악재": "📉", "중립": "➡️"}.get(ai_data.get('sentiment'), "❔")
            print(f"[{timestamp}] {sentiment_emoji} [분석] 스코어: {ai_data.get('sentiment_score', '-')}/10")
            print(f"  💎 헤드라인: {ai_data.get('headline')}")
            print(f"  🛡️ 신뢰도: {ai_data.get('source_type')}")
            print(f"  📌 관련종목: {', '.join(detected_stocks)}")
            print(f"  💬 분석근거: {ai_data.get('reason')}")
        
        first_line = next((l.strip() for l in raw_text.splitlines() if l.strip()), "")
        headline_for_store = str((ai_data or {}).get("headline") or first_line or "").strip()

        event_payload = {
            "timestamp": timestamp,
            "headline": headline_for_store,
            "ai_analysis": ai_data,
            "content": raw_text,
            "content_hash": content_hash,
            "publish_prices": publish_prices
        }

        # AI 분석 결과가 있고 점수가 4점 이상일 때만 푸시 발송
        score = (ai_data or {}).get("sentiment_score", 0) or 0
        if ai_data and score >= 4:
            await asyncio.to_thread(send_push_alert, event_payload, 4)
            
        await asyncio.to_thread(log_event_v2, event_payload)
    except Exception as e:
        print(f"  [Handler Error] {e}")

async def setup():
    try:
        entity = await client.get_entity(target_channel)
        client.add_event_handler(new_message_handler, events.NewMessage(chats=entity))
        print("✅ 실시간 수신 리스너 등록 완료.\n")
    except Exception as e:
        print(f"⚠️ 대상 채널 정보 확인 불가: {e}")

def main():
    print("=" * 50)
    print("🚀 실시간 데이터 분석 봇 실행 중")
    print("🤖 모든 수신 데이터는 데이터 중심의 중립적 분석을 수행합니다.")
    print("=" * 50)
    init_db()
    try:
        client.start()
        client.loop.run_until_complete(setup())
        client.run_until_disconnected()
    except Exception as e:
        if "locked" in str(e).lower():
            print("\n❌ [세션 데이터베이스 잠김] 다른 터미널에서 실행 중인 프로세스를 종료하세요.")
        else:
            print(f"\n❌ [오류 발생]: {e}")

if __name__ == '__main__':
    main()
