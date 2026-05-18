import os
import time
import datetime
import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

class SeleniumKRXScraper:
    def __init__(self, download_dir):
        self.download_dir = download_dir
        if not os.path.exists(self.download_dir):
            os.makedirs(self.download_dir)
            
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        
        # 다운로드 경로 설정
        prefs = {
            "download.default_directory": self.download_dir,
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True
        }
        chrome_options.add_experimental_option("prefs", prefs)
        
        self.driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
        self.wait = WebDriverWait(self.driver, 20)

    def _click_download(self, csv_button_selector="xpath=//button[contains(text(),'CSV')]"):
        """다운로드 버튼 클릭 및 파일 생성 대기"""
        # 1. 원본 다운로드 버튼 (엑셀/CSV 메뉴 호출)
        download_btn = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".btn_td_download")))
        download_btn.click()
        
        # 2. 'CSV' 버튼 선택
        csv_btn = self.wait.until(EC.element_to_be_clickable((By.LINK_TEXT, "CSV")))
        csv_btn.click()
        
        # 3. 파일 생성 대기 (KRX 특유의 '파일 생성 중' 오버레이)
        time.sleep(5) 

    def fetch_report(self, menu_id):
        url = f"http://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId={menu_id}"
        print(f"[{menu_id}] 페이지 접속 중...")
        self.driver.get(url)
        time.sleep(3) # 로딩 대기
        
        # '조회' 버튼 (Search) 클릭
        search_btn = self.wait.until(EC.element_to_be_clickable((By.ID, "jsSearchButton")))
        search_btn.click()
        time.sleep(2) # 데이터 렌더링 대기
        
        # 다운로드 실행
        self._click_download()
        print(f"[{menu_id}] 다운로드 요청 완료.")

    def close(self):
        self.driver.quit()

if __name__ == "__main__":
    DOWNLOAD_PATH = os.path.abspath("./data/raw_downloads")
    scraper = SeleniumKRXScraper(DOWNLOAD_PATH)
    
    try:
        # 1. 투자자 거래실적 (누적)
        scraper.fetch_report("MDC0201020302")
        
        # 2. 공매도 현황
        scraper.fetch_report("MDC0201030101")
        
        # 3. 대차잔고 현황
        scraper.fetch_report("MDC0201030201")
        
        print(f"다운로드 완료. 경로: {DOWNLOAD_PATH}")
    finally:
        scraper.close()
