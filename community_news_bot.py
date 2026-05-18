"""
community_news_bot.py
─────────────────────
속보 뉴스 → 디시 말투 변환 → 커뮤니티 글+댓글 자동 게시

사용법:
  python community_news_bot.py                   # Twitter 루프 (기본)
  python community_news_bot.py --test "제목"     # 제목 직접 테스트
"""

import os, re, json, time, random, argparse, requests, sys, io
from datetime import datetime, timedelta
from google import genai
from dotenv import load_dotenv

load_dotenv()

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ── 설정 ──────────────────────────────────────────────────────────
COMMUNITY_API = "http://localhost:8000/api/community"
# 2026-05-18: 하드코딩 키 제거 — .env 의 GEMINI_API_KEY 사용
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 봇이 쓸 닉네임 풀 (랜덤 픽)
BOT_NICKS = [
    "빠른 부엉이17", "차분한 여우63", "담대한 곰55", "성실한 너구리29",
    "유연한 수달41", "든든한 사슴07", "침착한 펭귄88", "현명한 독수리34",
    "집중한 토끼52", "강단있는 고양이16",
]

TOPIC_TAGS = ["종목", "시황", "잡담"]

client_genai = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# ── Gemini 호출 ───────────────────────────────────────────────────
def gemini(prompt: str) -> str:
    if not client_genai:
        return ""
    resp = client_genai.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=prompt,
    )
    return resp.text.strip()


# ── 디시 말투 글/댓글 생성 ────────────────────────────────────────
def generate_post_and_comments(news_title: str, news_body: str = "") -> dict:
    """뉴스를 기반으로 디시 말투 글 본문 + 댓글 2~3개 생성"""

    style_guide = """
디시인사이드 주식/해외주식 갤러리 유저 말투 특징:
- 짧고 단호한 문장, 개인 반응 위주
- ㅋㅋ ㄷㄷ ㄹㅇ ㅇㅇ 등 초성 자유롭게 사용
- "나 이거 들고있는데", "이거 매수해야하나", "ㄹㅇ 부럽다" 같은 주식 투자자 시선
- 뉴스를 자기 투자 상황/수익률과 연결지어 반응
- 가벼운 비속어(ㄹㅇ, 존나 등) 포함 가능, 심한 욕설 금지
- 이모티콘 없음, 영어 최소화
- 200자 이내로 짧게
- 절대 금지: 정치인 이름, 한국/해외 정치 언급, 철학/사상 관련 내용
"""

    # 1단계: 경제 영향 방향 먼저 분석
    prompt_analysis = f"""다음 뉴스가 주식/원자재/환율 시장에 미치는 영향을 딱 2줄로 분석해줘.
상승/하락 방향과 이유만. 틀리면 안 됨.

기본 경제 법칙 (반드시 따를 것):
- 중동 긴장 고조 / 전쟁 리스크 증가 → 유가 상승, 증시 하락
- 중동 긴장 완화 / 통행 정상화 → 유가 하락, 증시 상승
- 호르무즈/수에즈 통행량 증가 → 원유 공급 증가 → 유가 하락
- 금리 인상 → 증시 하락 / 금리 인하 → 증시 상승
- 달러 강세 → 원화 약세, 수출주 유리
- 실적 호조 → 해당 종목 상승

뉴스: {news_title}
{news_body}"""
    analysis = gemini(prompt_analysis)

    prompt_post = f"""{style_guide}

다음 주식/경제 속보와 시장 분석을 보고 디시인사이드 주식 갤러리 유저처럼 반응하는 글을 써줘.
원본 뉴스 그대로 옮기지 말고, 투자자 입장에서 자기 생각/반응을 써야 함.
시장 방향(상승/하락)은 아래 분석을 반드시 따를 것. 임의로 반대로 쓰지 말 것.
태그(#종목 등) 절대 붙이지 말고 본문만 바로 시작.

뉴스: {news_title}
시장 분석: {analysis}

글만 출력 (설명 없이):"""

    post_text = gemini(prompt_post)

    # 댓글 2~3개
    n_comments = random.randint(2, 3)
    prompt_comments = f"""{style_guide}

다음 주식 커뮤니티 글에 달릴 법한 댓글 {n_comments}개를 써줘.
각각 다른 투자자 입장 (공감, 자기 경험, 추가 의견 등) 으로.
시장 방향(상승/하락)은 아래 분석을 따를 것. 경제 논리를 틀리게 쓰지 말 것.
한 줄씩 출력. 번호나 기호 없이 댓글 내용만.

원본 뉴스: {news_title}
시장 분석: {analysis}
글: {post_text}

댓글 {n_comments}개 (각 줄에 하나씩):"""

    comments_raw = gemini(prompt_comments)
    comments = [c.strip() for c in comments_raw.split("\n") if c.strip()][:n_comments]

    return {"post": post_text, "comments": comments}


# ── 커뮤니티 API 게시 ─────────────────────────────────────────────
def post_to_community(content: str, author: str) -> dict:
    r = requests.post(f"{COMMUNITY_API}/posts",
        json={"content": content, "author": author}, timeout=10)
    r.raise_for_status()
    return r.json()


def add_comment(post_id: int, content: str, author: str, created_at: str = None) -> dict:
    payload = {"content": content, "author": author}
    if created_at:
        payload["created_at"] = created_at
    r = requests.post(f"{COMMUNITY_API}/posts/{post_id}/comments",
        json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


# ── 메인 게시 플로우 ──────────────────────────────────────────────
def publish_news(news_title: str, news_body: str = ""):
    print(f"\n[BOT] 뉴스 처리 중: {news_title}")

    generated = generate_post_and_comments(news_title, news_body)
    post_text  = generated["post"]
    comments   = generated["comments"]

    if not post_text:
        print("   [ERR] 글 생성 실패")
        return

    author = random.choice(BOT_NICKS)
    post   = post_to_community(post_text, author)
    post_id = post["id"]
    post_time = datetime.fromisoformat(post["created_at"])
    print(f"   [OK] 글 등록 (id={post_id}, author={author})")
    print(f"   본문: {post_text[:100]}...")

    # 댓글은 즉시 게시하되 자연스러운 랜덤 타임스탬프 적용
    # 첫 댓글: +3~10분, 이후 댓글: +5~20분씩 누적
    offset_min = 0
    for c_text in comments:
        gap = random.randint(3, 10) if offset_min == 0 else random.randint(5, 20)
        offset_min += gap
        c_time = (post_time + timedelta(minutes=offset_min)).isoformat(timespec="seconds")

        c_author = random.choice([n for n in BOT_NICKS if n != author])
        c = add_comment(post_id, c_text, c_author, created_at=c_time)
        print(f"   [댓글] (id={c['id']}, author={c_author}): {c_text[:60]}")

    print(f"   [완료] 글 1개 + 댓글 {len(comments)}개")


# ── Twitter 속보 감지 루프 ────────────────────────────────────────
def check_breaking_tweet(username: str, user_id: str):
    print(f"\n[@{username} 속보 확인 중...]")

    last_file = f"last_tweet_{username}.txt"
    last_id   = open(last_file).read().strip() if os.path.exists(last_file) else ""

    api_url = f"https://twitter241.p.rapidapi.com/user-tweets?user={user_id}&count=10"
    headers = {
        "x-rapidapi-host": "twitter241.p.rapidapi.com",
        "x-rapidapi-key":  os.environ.get("RAPIDAPI_KEY", ""),
    }

    try:
        r = requests.get(api_url, headers=headers, timeout=15)
        if r.status_code == 401:
            print(f"   [ERR] RAPIDAPI_KEY 없음 또는 만료됨 — .env에 RAPIDAPI_KEY 설정 필요")
            return
        if r.status_code == 429:
            print(f"   [SKIP] @{username} 요청 한도 초과 (429) — 다음 루프에서 재시도")
            return
        if r.status_code != 200:
            print(f"   [ERR] API 오류 ({r.status_code})")
            return

        data = r.json()
        tweets = []
        for inst in data.get("result", {}).get("timeline", {}).get("instructions", []):
            if inst.get("type") == "TimelineAddEntries":
                for entry in inst.get("entries", []):
                    item = entry.get("content", {}).get("itemContent", {})
                    res  = item.get("tweet_results", {}).get("result", {})
                    if res.get("__typename") == "Tweet":
                        tweets.append(res)

        for tweet in tweets:
            legacy   = tweet.get("legacy", {})
            tweet_id = legacy.get("id_str", "")
            text     = legacy.get("full_text", "")

            if "BREAKING" not in text:
                continue
            if tweet_id == last_id:
                print("   (새 속보 없음)")
                return

            # 정리
            clean = re.sub(r"https://\S+", "", text.replace("BREAKING:", "")).strip()
            print(f"   [속보] {clean[:80]}")

            publish_news(clean)

            with open(last_file, "w") as f:
                f.write(tweet_id)
            break

    except Exception as e:
        print(f"   [ERR] {e}")


# ── CLI ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", metavar="TITLE",
                        help="테스트 뉴스 제목 직접 입력")
    parser.add_argument("--interval", type=int, default=1800,
                        help="Twitter 체크 간격(초), 기본 1800(30분)")
    args = parser.parse_args()

    if args.test:
        publish_news(args.test)
    else:
        TARGET_ACCOUNTS = {
            "GlobeEyeNews": "1683054351647121408",
            "cryptorover":  "1353384573435056128",
        }
        print(f"[루프] Twitter 속보 루프 시작 (간격: {args.interval//60}분)")
        while True:
            for username, uid in TARGET_ACCOUNTS.items():
                check_breaking_tweet(username, uid)
            time.sleep(args.interval)
