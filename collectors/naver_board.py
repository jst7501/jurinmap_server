"""
naver_board.py — 네이버 종목토론방 감성 분석기
================================================
수집: 최근 5페이지(~100개) 게시글 제목 + 동의/반대 수
분석: 키워드 기반 감성 점수 (0~100)
       0  = 극도 절망 (역발상 매수 시그널)
       50 = 중립
       100 = 극도 환희 (역발상 매도 시그널)
"""

import re
import time
import requests
from bs4 import BeautifulSoup
from collections import Counter


# ─── 감성 키워드 사전 ──────────────────────────────────────
EUPHORIA_KEYWORDS = {
    # 강한 환희 (+3)
    '가즈아': 3, '영차영차': 3, '불장': 3, '슈퍼사이클': 3,
    '10배':  3, '텐배거': 3, '억대': 3, '주포': 3,
    '작전': 3, '세력입장': 3, '급등예정': 3, '폭발': 3,
    # 중간 환희 (+2)
    '매수추천': 2, '사자': 2, '줍줍': 2, '저점': 2,
    '돌파': 2, '신고가': 2, '강력매수': 2, '쌍끌이': 2,
    '기관매수': 2, '외국인매수': 2, '올라라': 2, '올라': 2,
    '상한가': 2, '급등': 2, '상승': 2, '훨훨': 2,
    '화이팅': 2, '파이팅': 2, '기대': 2, '가슴설렘': 2,
    # 약한 환희 (+1)
    '회복': 1, '반등': 1, '지지': 1, '버텨': 1,
    '매집': 1, '익절': 1, '수익': 1, '목표가': 1,
}

DESPAIR_KEYWORDS = {
    # 강한 절망 (-3)
    '한강': 3, '상폐': 3, '개잡주': 3, '사기': 3,
    '작전주': 3, '쓰레기': 3, '손절': 3, '탈출': 3,
    '망했': 3, '파산': 3, '폭락': 3, '하한가': 3,
    # 중간 절망 (-2)
    '팔아': 2, '던져': 2, '도망': 2, '실망': 2,
    '물렸': 2, '개잡': 2, '개소리': 2, '뻥': 2,
    '주담': 2, '악재': 2, '떨어져': 2, '하락': 2,
    '기관매도': 2, '외국인매도': 2, '손실': 2,
    # 약한 절망 (-1)
    '걱정': 1, '불안': 1, '조심': 1, '리스크': 1,
    '힘들': 1, '어렵': 1, '조정': 1, '약세': 1,
}

# 반대 키워드 (동의가 많을수록 감성 강화, 반대가 많으면 약화)
_DISAGREE_WEIGHT = 0.5


class NaverBoardCollector:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://finance.naver.com',
            'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8',
        })

    def fetch_posts(self, stock_code: str, pages: int = 5) -> list:
        """
        종목토론방 최근 N페이지 게시글 수집
        반환: [{'title': str, 'date': str, 'agree': int, 'disagree': int}, ...]
        """
        posts = []
        for page in range(1, pages + 1):
            url = f'https://finance.naver.com/item/board.naver?code={stock_code}&page={page}'
            try:
                res = self.session.get(url, timeout=10)
                if res.status_code != 200:
                    break
                soup = BeautifulSoup(res.text, 'html.parser')
                rows = soup.select('table.type2 tr')

                page_posts = []
                for r in rows:
                    tds = r.find_all('td')
                    if len(tds) < 6:
                        continue
                    title_a = tds[1].find('a')
                    if not title_a:
                        continue
                    title = title_a.text.strip()
                    if not title:
                        continue

                    try:
                        agree    = int(tds[3].text.strip() or '0')
                        disagree = int(tds[4].text.strip() or '0')
                    except ValueError:
                        agree, disagree = 0, 0

                    page_posts.append({
                        'title':    title,
                        'date':     tds[0].text.strip(),
                        'agree':    agree,
                        'disagree': disagree,
                    })

                posts.extend(page_posts)
                if len(page_posts) < 10:  # 마지막 페이지
                    break
                time.sleep(0.4)

            except Exception as e:
                print(f'  [Board] page {page} 오류: {e}')
                break

        return posts

    def analyze_sentiment(self, posts: list) -> dict:
        """
        수집한 게시글로 감성 점수 계산
        반환: {
            'score': 0~100 (0=절망, 50=중립, 100=환희),
            'mood': str,
            'grade': str,
            'raw_score': float,
            'post_count': int,
            'top_euphoria': [(keyword, count), ...],
            'top_despair':  [(keyword, count), ...],
            'agree_ratio':  float,
            'hot_posts':    [{'title', 'agree', 'disagree'}, ...],  # agree 상위 5
        }
        """
        if not posts:
            return self._empty_result()

        raw_score = 0.0
        total_weight = 0.0
        euphoria_counter = Counter()
        despair_counter  = Counter()

        for p in posts:
            text = p['title']
            agree    = p.get('agree', 0)
            disagree = p.get('disagree', 0)

            # 게시글 가중치: 동의수 많을수록 중요
            post_weight = 1.0 + agree * 0.3 - disagree * _DISAGREE_WEIGHT * 0.3
            post_weight = max(0.1, post_weight)

            post_score = 0
            for kw, val in EUPHORIA_KEYWORDS.items():
                if kw in text:
                    post_score += val
                    euphoria_counter[kw] += 1
            for kw, val in DESPAIR_KEYWORDS.items():
                if kw in text:
                    post_score -= val
                    despair_counter[kw] += 1

            raw_score    += post_score * post_weight
            total_weight += post_weight

        # 정규화: raw_score → 0~100
        if total_weight > 0:
            avg = raw_score / total_weight
        else:
            avg = 0

        # sigmoid-style normalization: avg ∈ [-5, +5] → score ∈ [0, 100]
        import math
        sigmoid = 1 / (1 + math.exp(-avg * 0.7))
        score = round(sigmoid * 100)

        # 동의 비율
        total_agree    = sum(p['agree'] for p in posts)
        total_disagree = sum(p['disagree'] for p in posts)
        agree_ratio = total_agree / (total_agree + total_disagree + 1) * 100

        # 인기 게시글 (동의 순)
        hot = sorted(posts, key=lambda x: x['agree'], reverse=True)[:5]

        # 감성 등급
        if score >= 80:
            mood, grade = '🔥 극도 환희', '매도 시그널'
        elif score >= 65:
            mood, grade = '😍 환희', '과열 주의'
        elif score >= 55:
            mood, grade = '🙂 낙관', '정상 상승장'
        elif score >= 45:
            mood, grade = '😐 중립', '관망 구간'
        elif score >= 35:
            mood, grade = '😟 불안', '저점 탐색'
        elif score >= 20:
            mood, grade = '😡 절망', '역발상 매수 고려'
        else:
            mood, grade = '☠️ 공황', '역발상 강매수 시그널'

        return {
            'score':         score,
            'mood':          mood,
            'grade':         grade,
            'raw_score':     round(avg, 2),
            'post_count':    len(posts),
            'agree_ratio':   round(agree_ratio, 1),
            'top_euphoria':  euphoria_counter.most_common(5),
            'top_despair':   despair_counter.most_common(5),
            'hot_posts':     hot,
        }

    def get_sentiment(self, stock_code: str, pages: int = 5) -> dict:
        """fetch + analyze 원스텝"""
        print(f'    > 종토방 수집 ({pages}페이지)... ', end='', flush=True)
        posts = self.fetch_posts(stock_code, pages)
        result = self.analyze_sentiment(posts)
        print(f'{result["post_count"]}건 | {result["mood"]} ({result["score"]}점)')
        return result

    @staticmethod
    def _empty_result() -> dict:
        return {
            'score': 50, 'mood': '😐 중립', 'grade': '데이터 없음',
            'raw_score': 0, 'post_count': 0, 'agree_ratio': 50,
            'top_euphoria': [], 'top_despair': [], 'hot_posts': [],
        }


# ─── 단독 실행 테스트 ─────────────────────────────────────
if __name__ == '__main__':
    import json
    from config.stock_list import TARGET_STOCKS

    collector = NaverBoardCollector()

    for code, info in list(TARGET_STOCKS.items())[:3]:
        name = info['name']
        print(f'\n=== {name} ({code}) ===')
        result = collector.get_sentiment(code, pages=3)
        print(f'  점수: {result["score"]}/100  |  {result["mood"]}')
        print(f'  환희 키워드: {result["top_euphoria"]}')
        print(f'  절망 키워드: {result["top_despair"]}')
        if result['hot_posts']:
            print(f'  인기글: {result["hot_posts"][0]["title"][:50]}  👍{result["hot_posts"][0]["agree"]}')
