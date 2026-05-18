"""
종토방 데이터를 수집하여 data.json에 통합하는 업데이트 스크립트
사용법: python scrapers/update_sentiment.py [종목코드] [페이지수]
예시:   python scrapers/update_sentiment.py 138080 5
"""
import sys
import json
import glob
import os
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from scrapers.naver_board_scraper import NaverBoardScraper


def find_latest_data_json():
    """data/ 디렉토리에서 가장 최근 stock_data_*.json 파일 찾기"""
    data_dir = ROOT / "data"
    files = sorted(data_dir.glob("stock_data_*.json"), reverse=True)
    if files:
        return files[0]
    # dashboard public도 체크
    public = ROOT / "dashboard" / "public" / "data.json"
    if public.exists():
        return public
    return None


def update_board_sentiment(stock_code, pages=5):
    print(f"\n[종토방 스크래핑 시작] 종목코드={stock_code}, {pages}페이지")

    # 1. 스크래핑 실행
    scraper = NaverBoardScraper()
    result = scraper.run(stock_code, pages=pages)

    if not result:
        print("[오류] 데이터를 가져올 수 없습니다.")
        return None

    print(f"\n✅ 수집 완료:")
    print(f"  - 총 게시글: {result['total_posts_scanned']}개")
    print(f"  - 감성 스코어: {result['score']}/100  ({result['mood']})")
    print(f"  - 환희 키워드: {result['euphoria_count']}건 / 절망 키워드: {result['despair_count']}건")
    print(f"\n📋 최신 게시글 TOP5:")
    for t in result['top_titles'][:5]:
        print(f"  - {t}")

    if result['top_euphoria']:
        print(f"\n🔥 환희 키워드 TOP3: {result['top_euphoria'][:3]}")
    if result['top_despair']:
        print(f"❄️  절망 키워드 TOP3: {result['top_despair'][:3]}")

    # 2. 결과를 별도 JSON으로 저장
    output_path = ROOT / "data" / f"board_sentiment_{stock_code}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n💾 저장 완료: {output_path}")

    # 3. 가장 최근 data.json 파일에도 병합
    data_file = find_latest_data_json()
    if data_file:
        print(f"\n🔄 데이터 파일 업데이트 중: {data_file}")
        with open(data_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 딕셔너리 형태: { "046970": {...}, "138080": {...} }
        if stock_code in data:
            data[stock_code]["board_sentiment"] = result
            print(f"  → [{stock_code}] 항목에 board_sentiment 추가")
        else:
            # 단일 종목 파일이거나 _macro 같은 구조일 경우
            data["board_sentiment"] = result
            print(f"  → 최상위에 board_sentiment 추가 (종목코드 키 없음)")

        with open(data_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        print(f"✅ {data_file} 업데이트 완료!")

    # dashboard public/data.json도 업데이트
    public_data = ROOT / "dashboard" / "public" / "data.json"
    if public_data.exists() and public_data != data_file:
        print(f"\n🔄 대시보드 데이터 업데이트 중: {public_data}")
        with open(public_data, "r", encoding="utf-8") as f:
            pub = json.load(f)

        if stock_code in pub:
            pub[stock_code]["board_sentiment"] = result
            print(f"  → [{stock_code}] 항목에 board_sentiment 추가")
        else:
            pub["board_sentiment"] = result

        with open(public_data, "w", encoding="utf-8") as f:
            json.dump(pub, f, ensure_ascii=False, indent=4)
        print(f"✅ 대시보드 public/data.json 업데이트 완료!")

    return result


if __name__ == "__main__":
    code = sys.argv[1] if len(sys.argv) > 1 else "138080"
    pages = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    update_board_sentiment(code, pages)
