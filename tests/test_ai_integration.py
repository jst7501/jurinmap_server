import sys
import os
from pathlib import Path

# Add project root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scrapers.ai_analysis_engine import HumanIndicatorAI
from scrapers.update_all import _fetch_board_page, scrape_board

def test_ai():
    code = "138080"
    name = "오이솔루션"
    print(f"Testing {name} ({code})...")
    
    # 1. Fetch
    posts = _fetch_board_page(code, 1)
    print(f"Fetched {len(posts)} posts.")
    
    # 2. Scrape & AI
    result = scrape_board(code, name, pages=1)
    print("Result analysis done.")
    
    # 3. AI Check
    if "ai_insight" in result:
        print("AI result found:")
        print(result["ai_insight"])
    else:
        print("AI result NOT found.")

if __name__ == "__main__":
    test_ai()
