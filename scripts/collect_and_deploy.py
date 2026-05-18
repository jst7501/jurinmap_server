#!/usr/bin/env python3
"""
collect_and_deploy.py
───────────────────────────────────────────────────────────
1. python main.py 실행 (KIS API 데이터 수집)
2. 생성된 최신 JSON을 dashboard/public/data.json에 복사
3. 브라우저는 yarn dev 후 http://localhost:5173 에서 바로 확인

사용법:
    python scripts/collect_and_deploy.py
"""
import subprocess
import shutil
import glob
import os
from datetime import datetime

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    print("─" * 50)
    print(" PULSE TERMINAL — 데이터 수집 & 배포")
    print("─" * 50)

    # Step 1: 데이터 수집
    print("\n[1/2] 데이터 수집 시작 (main.py)...")
    result = subprocess.run(["python", "main.py"], cwd=ROOT_DIR)
    if result.returncode != 0:
        print("❌ 수집 중 오류 발생. data/ 폴더를 확인해주세요.")
        return

    # Step 2: 최신 JSON 복사
    print("\n[2/2] 최신 JSON → dashboard/public/data.json 복사...")
    data_files = sorted(glob.glob(os.path.join(ROOT_DIR, "data", "stock_data_*.json")), reverse=True)
    if not data_files:
        print("❌ data/ 폴더에 JSON 파일이 없습니다.")
        return

    latest = data_files[0]
    dest = os.path.join(ROOT_DIR, "dashboard", "public", "data.json")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(latest, dest)

    size_kb = os.path.getsize(dest) / 1024
    print(f"✅ 완료: {os.path.basename(latest)} → dashboard/public/data.json ({size_kb:.1f} KB)")
    print(f"\n🚀 이제 'yarn dev' 후 http://localhost:5173 에서 확인하세요.")
    print("─" * 50)


if __name__ == "__main__":
    main()
