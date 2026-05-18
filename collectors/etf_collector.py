import requests
import datetime
import json
import re
import sys
import time
import logging
from pathlib import Path
from bs4 import BeautifulSoup

# Add project root to sys.path to import get_stocks_conn
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.state import get_stocks_conn

logger = logging.getLogger("collectors.etf")

class ETFCollector:
    def __init__(self):
        # Naver Finance ETF API
        self.url = "https://finance.naver.com/api/sise/etfItemList.nhn"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        self.category_map = {
            1: "국내지수",
            2: "국내테마",
            3: "국내파생",
            4: "해외주식",
            5: "원자재",
            6: "채권",
            7: "기타"
        }

    def fetch_kr_etf_list(self):
        """네이버 금융에서 전체 ETF 시세 및 NAV 데이터를 가져옵니다."""
        try:
            res = requests.get(self.url, headers=self.headers, timeout=10)
            if res.status_code == 200:
                data = res.json()
                items = data.get("result", {}).get("etfItemList", [])
                if items:
                    # 네이버는 당일 시점 데이터를 주므로 현재 날짜 사용
                    asof_date = datetime.datetime.now().strftime("%Y%m%d")
                    return items, asof_date
            print(f"DEBUG: Naver API Error. Status: {res.status_code}")
        except Exception as e:
            print(f"DEBUG: Fetch Error: {e}")
        return [], None

    def save_to_db(self, etf_list, asof_date):
        if not etf_list:
            return
            
        conn = get_stocks_conn()
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS kr_etf_master (
                code TEXT PRIMARY KEY,
                name TEXT,
                category TEXT,
                price BIGINT,
                change_amt BIGINT,
                change_rate DOUBLE PRECISION,
                nav DOUBLE PRECISION,
                volume BIGINT,
                amount BIGINT,
                market_cap BIGINT,
                asof_date TEXT,
                updated_at TEXT
            )
        """)
        
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        insert_data = []
        for item in etf_list:
            code = item.get("itemcode", "")
            if not code: continue
            
            tab_code = item.get("etfTabCode", 7)
            category = self.category_map.get(tab_code, "기타")
            
            price = int(item.get("nowVal") or 0)
            change_amt = int(item.get("changeVal") or 0)
            change_rate = float(item.get("changeRate") or 0.0)
            nav = float(item.get("nav") or 0.0)
            volume = int(item.get("quant") or 0)
            amount = int(item.get("amonut") or 0) * 1_000_000
            market_cap = int(item.get("marketSum") or 0) * 100_000_000
            
            insert_data.append((
                code, item.get("itemname"), category, price, change_amt, 
                change_rate, nav, volume, amount, market_cap, asof_date, now
            ))
            
        # PgCompatConnection.executemany rewrites '?' → '%s' for Postgres.
        cursor.executemany("""
            INSERT INTO kr_etf_master (
                code, name, category, price, change_amt, change_rate, nav, 
                volume, amount, market_cap, asof_date, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name=excluded.name,
                category=excluded.category,
                price=excluded.price,
                change_amt=excluded.change_amt,
                change_rate=excluded.change_rate,
                nav=excluded.nav,
                volume=excluded.volume,
                amount=excluded.amount,
                market_cap=excluded.market_cap,
                asof_date=excluded.asof_date,
                updated_at=excluded.updated_at
        """, insert_data)
        
        conn.commit()
        conn.close()
        print(f"Successfully saved {len(insert_data)} ETFs to DB using active connection.")

    # ─── coinfo 메타데이터 크롤링 ────────────────────────────────
    def fetch_etf_meta(self, code):
        """네이버 coinfo 페이지에서 기초지수, 유형, 보수, 운용사, 수익률 추출"""
        try:
            url = f"https://finance.naver.com/item/coinfo.naver?code={code}"
            r = requests.get(url, headers=self.headers, timeout=10)
            soup = BeautifulSoup(r.content.decode("euc-kr", errors="replace"), "html.parser")
            tables = soup.select("table")
            meta = {"code": code}

            # t[2] 기초지수, 유형, 상장일
            if len(tables) > 2:
                for tr in tables[2].select("tr"):
                    ths = " ".join(th.text.strip() for th in tr.select("th"))
                    tds = [td.text.strip() for td in tr.select("td")]
                    if "기초지수" in ths and tds:
                        meta["base_index"] = tds[0]
                    if "유형" in ths and tds:
                        meta["etf_type"] = tds[0]
                    if "상장일" in ths and tds:
                        meta["listed_date"] = tds[0]

            # t[3] 보수, 운용사
            if len(tables) > 3:
                for tr in tables[3].select("tr"):
                    ths = " ".join(th.text.strip() for th in tr.select("th"))
                    tds = [td.text.strip() for td in tr.select("td")]
                    if "보수" in ths and tds:
                        meta["total_fee"] = tds[0]
                    if "운용사" in ths and tds:
                        meta["asset_manager"] = tds[0]

            # t[5] 수익률
            if len(tables) > 5:
                ret_tds = []
                for tr in tables[5].select("tr"):
                    ret_tds.extend(td.text.strip() for td in tr.select("td"))
                labels = ["return_1m", "return_3m", "return_6m", "return_1y"]
                for i, label in enumerate(labels):
                    if i < len(ret_tds):
                        meta[label] = ret_tds[i]
            return meta
        except Exception as e:
            logger.debug("[etf-meta] %s error: %s", code, e)
            return {"code": code}

    # ─── wisereport 구성종목 크롤링 ────────────────────────────
    def fetch_etf_holdings(self, code):
        """wisereport 인라인 CU_data JSON에서 구성종목 추출"""
        try:
            url = f"https://navercomp.wisereport.co.kr/v2/ETF/index.aspx?cmp_cd={code}&target=etf_pdf"
            r = requests.get(url, headers=self.headers, timeout=10)
            text = r.content.decode("utf-8", errors="replace")
            match = re.search(r"CU_data\s*=\s*(\{.*?\});", text, re.DOTALL)
            if not match:
                return []
            cu = json.loads(match.group(1))
            grid = cu.get("grid_data", [])
            return [
                {
                    "etf_code": code,
                    "stock_name": item.get("STK_NM_KOR", ""),
                    "weight": round(item.get("ETF_WEIGHT", 0), 2),
                    "shares": item.get("AGMT_STK_CNT", 0),
                    "trade_date": item.get("TRD_DT", ""),
                }
                for item in grid
                if item.get("STK_NM_KOR")
            ]
        except Exception as e:
            logger.debug("[etf-holdings] %s error: %s", code, e)
            return []

    # ─── 전체 ETF 배치 크롤링 ──────────────────────────────────
    def crawl_all_etf_details(self, sleep_sec=0.3):
        """kr_etf_master의 모든 ETF에 대해 메타 + 구성종목 크롤링 후 DB 저장"""
        conn = get_stocks_conn()
        try:
            rows = conn.execute("SELECT code FROM kr_etf_master WHERE price > 0 ORDER BY amount DESC").fetchall()
        finally:
            conn.close()

        codes = [r[0] if not hasattr(r, "keys") else r["code"] for r in rows]
        logger.info("[etf-crawl] 총 %d개 ETF 크롤링 시작", len(codes))

        all_meta = []
        all_holdings = []

        for i, code in enumerate(codes):
            meta = self.fetch_etf_meta(code)
            all_meta.append(meta)
            time.sleep(sleep_sec)

            holdings = self.fetch_etf_holdings(code)
            all_holdings.extend(holdings)
            time.sleep(sleep_sec)

            if (i + 1) % 50 == 0:
                logger.info("[etf-crawl] %d/%d 완료", i + 1, len(codes))

        # DB 저장
        self._save_meta_to_db(all_meta)
        self._save_holdings_to_db(all_holdings)
        logger.info("[etf-crawl] 완료: meta %d건, holdings %d건", len(all_meta), len(all_holdings))
        return {"meta": len(all_meta), "holdings": len(all_holdings)}

    def _save_meta_to_db(self, meta_list):
        if not meta_list:
            return
        conn = get_stocks_conn()
        try:
            # 칼럼 마이그레이션
            for col, col_type in [
                ("base_index", "TEXT"), ("etf_type", "TEXT"), ("listed_date", "TEXT"),
                ("asset_manager", "TEXT"), ("total_fee", "TEXT"),
                ("return_1m", "TEXT"), ("return_3m", "TEXT"),
                ("return_6m", "TEXT"), ("return_1y", "TEXT"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE kr_etf_master ADD COLUMN {col} {col_type}")
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass

            for m in meta_list:
                code = m.get("code")
                if not code:
                    continue
                conn.execute("""
                    UPDATE kr_etf_master SET
                        base_index = ?, etf_type = ?, listed_date = ?,
                        asset_manager = ?, total_fee = ?,
                        return_1m = ?, return_3m = ?, return_6m = ?, return_1y = ?
                    WHERE code = ?
                """, (
                    m.get("base_index"), m.get("etf_type"), m.get("listed_date"),
                    m.get("asset_manager"), m.get("total_fee"),
                    m.get("return_1m"), m.get("return_3m"), m.get("return_6m"), m.get("return_1y"),
                    code,
                ))
            conn.commit()
        finally:
            conn.close()

    def _save_holdings_to_db(self, holdings_list):
        if not holdings_list:
            return
        conn = get_stocks_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS etf_holdings (
                    etf_code TEXT NOT NULL,
                    stock_name TEXT NOT NULL,
                    weight DOUBLE PRECISION,
                    shares DOUBLE PRECISION,
                    trade_date TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (etf_code, stock_name)
                )
            """)
            conn.commit()

            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # 기존 데이터 삭제 후 재삽입 (전체 갱신)
            etf_codes = list(set(h["etf_code"] for h in holdings_list))
            for ec in etf_codes:
                conn.execute("DELETE FROM etf_holdings WHERE etf_code = ?", (ec,))

            for h in holdings_list:
                conn.execute("""
                    INSERT INTO etf_holdings (etf_code, stock_name, weight, shares, trade_date, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (h["etf_code"], h["stock_name"], h["weight"], h["shares"], h["trade_date"], now))
            conn.commit()
        finally:
            conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    collector = ETFCollector()
    etfs, date = collector.fetch_kr_etf_list()
    if etfs:
        collector.save_to_db(etfs, date)
        print(f"ETF 시세 {len(etfs)}건 저장 완료")
    # 전체 메타+구성종목 크롤링
    result = collector.crawl_all_etf_details(sleep_sec=0.3)
    print(f"크롤링 완료: {result}")
