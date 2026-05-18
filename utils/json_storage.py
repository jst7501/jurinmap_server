import os
import json
from config.settings import DATA_DIR

def save_to_json(filename, data):
    filepath = os.path.join(DATA_DIR, filename)
    
    # 만약 파일이 존재하고, data가 리스트라면 기존 리스트에 추가(append)하는 방식도 고려할 수 있지만
    # 일단은 전체를 덮어쓰거나, 날짜별로 파일을 생성한다고 가정 (베타 버전)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        
def append_to_jsonl(filename, data):
    """
    JSON Lines 형식으로 데이터 추가 (로그/이력 관리에 용이)
    """
    filepath = os.path.join(DATA_DIR, filename)
    with open(filepath, 'a', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
        f.write('\n')

def read_json(filename):
    filepath = os.path.join(DATA_DIR, filename)
    if not os.path.exists(filepath):
        return None
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)
