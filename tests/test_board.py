import sys
from scrapers.naver_board_scraper import NaverBoardScraper
import json

scraper = NaverBoardScraper()
posts = scraper.scrape('138080', pages=2)
print(f'수집된 게시글: {len(posts)}개')
for p in posts[:5]:
    print(f'  [{p["views"]}뷰] {p["title"]} ({p["date"]})')
result = scraper.analyze_sentiment(posts)
print(f'\n감성 스코어: {result["score"]}/100 ({result["mood"]})')
print(f'환희 키워드 {result["euphoria_count"]}건 / 절망 키워드 {result["despair_count"]}건')
print('\n최신 게시글:')
for t in result['top_titles']:
    print(f'  - {t}')
