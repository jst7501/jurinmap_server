import sys
import os

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

import json
from datetime import datetime
from collectors.kis_api import KISCollector


def fetch_and_save_top100():
    # Initialize the collector
    kis = KISCollector()

    print("Fetching KOSPI TOP 100...")
    kospi_top100 = kis.get_transaction_value_ranking("0001")

    print("Fetching KOSDAQ TOP 100...")
    kosdaq_top100 = kis.get_transaction_value_ranking("1001")

    data = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "kospi": kospi_top100,
        "kosdaq": kosdaq_top100
    }

    out_path = os.path.join(ROOT_DIR, "data", "top_100_trade_value.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Successfully saved to {out_path}.")
    print(f"KOSPI count: {len(kospi_top100)}, KOSDAQ count: {len(kosdaq_top100)}")


if __name__ == "__main__":
    fetch_and_save_top100()
