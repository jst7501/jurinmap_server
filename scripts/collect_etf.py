import sys
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collectors.etf_collector import ETFCollector

def main():
    print("Starting ETF collection (Postgres via server.db.connections)...")

    collector = ETFCollector()
    etfs, date = collector.fetch_kr_etf_list()
    
    if etfs:
        print(f"Fetched {len(etfs)} ETFs for date {date}")
        collector.save_to_db(etfs, date)
        print("ETF collection completed.")
    else:
        print("Error: No ETF data found. Check network or KRX API status.")
        sys.exit(1)

if __name__ == "__main__":
    main()
