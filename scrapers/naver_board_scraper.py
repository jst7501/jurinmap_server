"""
네이버 종목 토론방 (종토방) 스크레이퍼 + 감성 분석 엔진
- 최근 5페이지(약 100개 게시글) 데이터 수집
- 제목, 작성일, 조회수, 공감/비공감 수집
- 주식 전용 은어(Slang) 기반 감성 스코어링
"""
import requests
from bs4 import BeautifulSoup
import json
import datetime
import re
import time
import sys


class NaverBoardScraper:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://finance.naver.com/"
        }
        # 감성 분석 키워드 사전 (주식 전용 인터넷 은어)
        self.KEYWORDS = {
            "euphoria": [
                "가즈아", "가자", "상한가", "풀매수", "대박", "축제", "호재",
                "점상", "폭등", "익절", "저점매수", "줍줍", "불꽃", "쭉가자",
                "존버", "매집", "세력", "목표가", "급등", "오늘도상한가"
            ],
            "despair": [
                "한강", "망함", "손절", "개미지옥", "상폐", "떡락", "물림",
                "지옥", "설거지", "패닉", "곡소리", "살려줘", "털림", "개박살",
                "반토막", "물렸다", "개같다", "ㅡㅜ", "ㅠㅠ", "하락", "폭락", "쓰레기"
            ]
        }

    def fetch_posts(self, code, pageSize=20, offset=None):
        url = "https://stock.naver.com/api/community/discussion/posts/by-item"
        params = {
            "discussionType": "domesticStock",
            "itemCode": code,
            "isHolderOnly": "false",
            "excludesItemNews": "false",
            "isItemNewsOnly": "false",
            "isCleanbotPassedOnly": "false",
            "pageSize": pageSize
        }
        if offset:
            params["fromPostId"] = offset # 페이징 처리용 (API에 따라 offset 또는 fromPostId 사용)
            
        try:
            r = requests.get(url, params=params, headers=self.headers, timeout=10)
            return r.json()
        except Exception as e:
            print(f"  [!] API 호출 오류: {e}")
            return None

    def parse_api_posts(self, data):
        """API 응답 JSON에서 게시글 목록 추출"""
        if not data or 'posts' not in data:
            return []
            
        parsed = []
        for p in data['posts']:
            # 날짜 포맷 정리 (2026-04-06T08:21:07 -> 2026.04.06 08:21)
            raw_date = p.get('writtenAt', '')
            date_clean = raw_date.replace('T', ' ')[:16].replace('-', '.') if raw_date else ""
            
            parsed.append({
                "title": p.get('title', ''),
                "date": date_clean,
                "views": 0, # API에서 제공하지 않음
                "replies": p.get('commentCount', 0),
                "good": p.get('recommendCount', 0),
                "bad": p.get('notRecommendCount', 0)
            })
        return parsed

    def scrape(self, code, pages=5):
        """최근 N 페이지 상당의 종토방 게시글 API 수집"""
        all_posts = []
        pageSize = 20
        total_to_fetch = pages * pageSize # 이전 방식과 유사한 개수 유지
        
        print(f"[{code}] 종토방 API 수집 중 (목표: 약 {total_to_fetch}개)...")
        
        offset = None
        while len(all_posts) < total_to_fetch:
            data = self.fetch_posts(code, pageSize=pageSize, offset=offset)
            if not data:
                break
                
            posts = self.parse_api_posts(data)
            if not posts:
                break
                
            all_posts.extend(posts)
            print(f"  → 현재 {len(all_posts)}개 수집 완료")
            
            offset = data.get('lastOffset')
            if not offset:
                break
            time.sleep(0.2) # 서버 부하 방지
            
        return all_posts[:total_to_fetch]

    def analyze_sentiment(self, posts):
        """게시글 제목 기반 감성 점수 계산"""
        euphoria_cnt = 0
        despair_cnt = 0
        top_euphoria = {}
        top_despair = {}
        titles = [p["title"] for p in posts]

        for title in titles:
            for kw in self.KEYWORDS["euphoria"]:
                if kw in title:
                    euphoria_cnt += 1
                    top_euphoria[kw] = top_euphoria.get(kw, 0) + 1
            for kw in self.KEYWORDS["despair"]:
                if kw in title:
                    despair_cnt += 1
                    top_despair[kw] = top_despair.get(kw, 0) + 1

        # 0~100 점수 계산 (중립 50 기준)
        total = euphoria_cnt + despair_cnt
        if total > 0:
            # 부드러운 스코어링: 비율 기반
            ratio = euphoria_cnt / total  # 0 ~ 1
            score = int(ratio * 100)
        else:
            score = 50

        mood = "중립"
        if score >= 75: mood = "환희 🔥 (고점 주의)"
        elif score >= 60: mood = "낙관 😊"
        elif score <= 25: mood = "공포 😱 (역발상 기회?)"
        elif score <= 40: mood = "불안 😰"

        # 가장 화제가 되는 게시글 TOP8 (조회수가 없으므로 댓글 + 공감순으로 선정)
        top_posts = sorted(posts, key=lambda p: (p["replies"] * 2 + int(p["good"])), reverse=True)[:8]

        return {
            "score": score,
            "mood": mood,
            "total_posts_scanned": len(posts),
            "euphoria_count": euphoria_cnt,
            "despair_count": despair_cnt,
            "top_titles": [p["title"] for p in posts[:8]],           # 최신 8개
            "top_hot_posts": top_posts,                                # 조회수 TOP8
            "top_euphoria": sorted(top_euphoria.items(), key=lambda x: x[1], reverse=True),
            "top_despair": sorted(top_despair.items(), key=lambda x: x[1], reverse=True),
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

    def run(self, code, pages=5):
        """메인 엔트리 포인트: 스크래핑 + 분석 + 결과 반환"""
        posts = self.scrape(code, pages)
        if not posts:
            return None
        result = self.analyze_sentiment(posts)
        return result


if __name__ == "__main__":
    code = sys.argv[1] if len(sys.argv) > 1 else "138080"
    pages = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    scraper = NaverBoardScraper()
    result = scraper.run(code, pages)

    if result:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(json.dumps({"error": "데이터를 가져올 수 없습니다."}))
