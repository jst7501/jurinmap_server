"""
사용자가 직접 제공한 코스피/코스닥 거래량 TOP 100 리스트를
DART 기업코드 마스터와 매핑하여 JSON 파일로 저장합니다.
ETF(KODEX, TIGER, KBSTAR 등)는 자동 필터링됩니다.
"""
import sys, os, json, requests, zipfile, io, xml.etree.ElementTree as ET
from datetime import datetime

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
from config.settings import DART_API_KEY

# ── 수동 오버라이드 (DART 검색명 vs 표시명 불일치 보정) ──────
MANUAL_CODE = {
    "현대차2우B":         "005387",
    "JYP Ent.":           "035900",
    "NAVER":              "035420",
    "LS ELECTRIC":        "010120",
    "POSCO홀딩스":         "005490",
    "HD현대중공업":         "329180",
    "HD현대일렉트릭":       "267260",
    "HD한국조선해양":       "009540",
    "HD현대":             "267250",
    "HD현대마린솔루션":     "443060",
    "HD건설기계":          "267270",
    "S-Oil":              "010950",
    "HLB":                "028300",
    "HPSP":               "403870",
    "RFHIC":              "218410",
    "CJ ENM":             "035760",
    "HK이노엔":            "083790",
    "HMM":                "011200",
    "KB금융":             "105560",
    "KT&G":               "033780",
    "KT":                 "030200",
    "LS":                 "006260",
    "LG":                 "003550",
    "SK":                 "034730",
    "DB손해보험":          "005830",
}

# ── 코스닥 100 (ETF 없음) ───────────────────────────────────
KOSDAQ_100 = [
    "알테오젠","에코프로","에코프로비엠","삼천당제약","레인보우로보틱스",
    "에이비엘바이오","코오롱티슈진","리노공업","HLB","펩트론",
    "리가켐바이오","원익IPS","ISC","보로노이","케어젠",
    "이오테크닉스","펄어비스","로보티즈","우리기술","HPSP",
    "클래시스","올릭스","성호전자","에임드바이오","파마리서치",
    "휴젤","현대무벡스","디앤디파마텍","솔브레인","에스티팜",
    "주성엔지니어링","비에이치아이","서진시스템","유진테크","메지온",
    "파두","알지노믹스","셀트리온제약","동진쎄미켐","실리콘투",
    "티씨케이","에스피지","RFHIC","피에스케이","JYP Ent.",
    "스피어","대한광통신","심텍","원익홀딩스","피에스케이홀딩스",
    "에스엠","하나마이크론","대주전자재료","오스코텍","두산테스나",
    "고영","로킷헬스케어","쎄트렉아이","테크윙","비츠로셀",
    "에스앤에스텍","오름테라퓨틱","삼표시멘트","파크시스템스","리브스메드",
    "엘앤씨바이오","태성","차바이오텍","LS마린솔루션","삼현",
    "하림지주","신성델타테크","미래에셋벤처투자","와이씨","큐리옥스바이오시스템즈",
    "동국제약","제이에스링크","LS머트리얼즈","제주반도체","HK이노엔",
    "레이크머티리얼즈","큐리언트","젬백스","휴림로봇","현대바이오",
    "테스","인텔리안테크","티에스이","코미코","CJ ENM",
    "씨젠","케이엠더블유","에이프릴바이오","태광","피엔티",
    "씨엠티엑스","덕산네오룩스","아이티센글로벌","네이처셀","지투지바이오",
]

# ── 코스피 100 (ETF 자동 제거됨 → 실제 91개) ────────────────
KOSPI_100_RAW = [
    "삼성전자","SK하이닉스","삼성전자우","LG에너지솔루션","현대차",
    "한화에어로스페이스","삼성바이오로직스","SK스퀘어","두산에너빌리티","기아",
    "KB금융","HD현대중공업","셀트리온","삼성생명","신한지주",
    "삼성물산","한화오션","삼성SDI","현대모비스","미래에셋증권",
    "삼성전기","HD현대일렉트릭","하나금융지주","고려아연","NAVER",
    "POSCO홀딩스","HD한국조선해양","한국전력","효성중공업","한미반도체",
    "한화시스템","LS ELECTRIC","삼성중공업","우리금융지주","현대로템",
    "SK","LG화학","삼성화재","SK이노베이션","카카오",
    "HD현대","메리츠금융지주","HMM","포스코퓨처엠",
    # rank 45: KODEX 200 (ETF - 제외)
    "KT&G","한국항공우주","LG전자","LIG넥스원","두산",
    "SK텔레콤","기업은행","현대건설","현대글로비스","KT",
    # rank 56: TIGER 미국S&P500 (ETF - 제외)
    "포스코인터내셔널","LG","S-Oil","에이피알","삼성에피스홀딩스",
    "한국금융지주","삼성에스디에스","하이브","DB손해보험","카카오뱅크",
    "크래프톤","키움증권","NH투자증권","현대오토에버",
    # rank 71: TIGER 반도체TOP10 (ETF - 제외)
    "삼양식품","삼성E&A","삼성증권","한화","대한항공",
    "LS","현대차2우B",
    # rank 79,80,81: KODEX CD금리/TIGER 나스닥/KODEX S&P500 (ETF - 제외)
    "LG이노텍","HD현대마린솔루션",
    # rank 84: KODEX 머니마켓 (ETF - 제외)
    "이수페타시스","아모레퍼시픽","SK바이오팜","유한양행","한진칼",
    # rank 90: TIGER 200 (ETF - 제외)
    "HD건설기계","대우건설","엘앤에프","카카오페이","LG유플러스",
    "한화솔루션","한국타이어앤테크놀로지","한미약품",
    # rank 99: KODEX 코스닥150 (ETF - 제외)
    "한전기술",
]

def load_dart_map():
    print("▶ DART 기업코드 마스터 로딩 중...", end=" ")
    url = "https://opendart.fss.or.kr/api/corpCode.xml"
    res = requests.get(url, params={"crtfc_key": DART_API_KEY}, timeout=15)
    name_to_code = {}
    if res.status_code == 200:
        with zipfile.ZipFile(io.BytesIO(res.content)) as z:
            with z.open('CORPCODE.xml') as f:
                root = ET.parse(f).getroot()
                for lst in root.findall('list'):
                    sc = lst.find('stock_code').text
                    nm = lst.find('corp_name').text
                    if sc and sc.strip():
                        name_to_code[nm.strip()] = sc.strip()
    print(f"완료 ({len(name_to_code)}개)")
    return name_to_code

def resolve(name, name_to_code):
    if name in MANUAL_CODE:
        return MANUAL_CODE[name]
    if name in name_to_code:
        return name_to_code[name]
    # 부분 매칭 시도
    for k, v in name_to_code.items():
        if name in k or k in name:
            return v
    return None

def main():
    name_to_code = load_dart_map()

    result = {"kospi": [], "kosdaq": [], "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    unresolved = []

    for name in KOSDAQ_100:
        code = resolve(name, name_to_code)
        if code:
            result["kosdaq"].append({"code": code, "name": name, "market": "코스닥"})
        else:
            unresolved.append(f"[코스닥] {name}")

    for name in KOSPI_100_RAW:
        code = resolve(name, name_to_code)
        if code:
            result["kospi"].append({"code": code, "name": name, "market": "코스피"})
        else:
            unresolved.append(f"[코스피] {name}")

    print(f"✅ 코스피 {len(result['kospi'])}개, 코스닥 {len(result['kosdaq'])}개 매핑 완료")

    if unresolved:
        print(f"⚠️  코드 미확인 종목 {len(unresolved)}개:")
        for u in unresolved:
            print(f"   - {u}")

    out_path = os.path.join(ROOT_DIR, "data", "top100_stock_list.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n💾 저장 완료: {out_path}")
    print(f"   총 {len(result['kospi']) + len(result['kosdaq'])}개 종목")

if __name__ == "__main__":
    main()
