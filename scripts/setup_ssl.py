"""
로컬 HTTPS 인증서 생성 스크립트
localhost용 자체 서명 인증서를 만들어 server/certs/ 에 저장합니다.

실행: python scripts/setup_ssl.py
이후: uvicorn server.app_server:app --ssl-keyfile server/certs/key.pem --ssl-certfile server/certs/cert.pem --reload
"""
import os, sys, subprocess

ROOT_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CERTS_DIR = os.path.join(ROOT_DIR, "server", "certs")
KEY_FILE  = os.path.join(CERTS_DIR, "key.pem")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")

os.makedirs(CERTS_DIR, exist_ok=True)

if os.path.exists(KEY_FILE) and os.path.exists(CERT_FILE):
    print("[OK] 인증서가 이미 존재합니다:")
    print(f"  key  : {KEY_FILE}")
    print(f"  cert : {CERT_FILE}")
    sys.exit(0)

print("인증서 생성 중 (openssl 필요)...")

try:
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", KEY_FILE,
        "-out",    CERT_FILE,
        "-days",   "825",
        "-nodes",
        "-subj",   "/CN=localhost",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ], check=True, capture_output=True)
    print("[OK] 인증서 생성 완료!")
    print(f"  key  : {KEY_FILE}")
    print(f"  cert : {CERT_FILE}")
    print()
    print("서버 실행 명령:")
    print(f"  uvicorn server.app_server:app --ssl-keyfile {KEY_FILE} --ssl-certfile {CERT_FILE} --reload")
    print()
    print("주의: 처음 브라우저에서 열 때 '신뢰할 수 없는 인증서' 경고가 뜹니다.")
    print("  Chrome: 경고 화면에서 '고급' → 'localhost(으)로 이동' 클릭 한 번만 하면 됩니다.")
except FileNotFoundError:
    print("[WARN] openssl 명령을 찾을 수 없습니다.")
    print()
    print("대안: GitHub Pages(HTTPS) → http://localhost:8000 은")
    print("최신 Chrome/Firefox/Edge에서 localhost 예외로 허용됩니다.")
    print("SSL 없이도 대부분 정상 동작합니다.")
    print()
    print("openssl 설치 방법:")
    print("  Windows: https://slproweb.com/products/Win32OpenSSL.html")
    print("  또는 Git for Windows 설치 시 포함됨 (Git Bash 사용)")
except subprocess.CalledProcessError as e:
    print(f"[ERROR] 인증서 생성 실패: {e.stderr.decode()}")
