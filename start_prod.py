"""
start_prod.py
프로덕션 서버 시작 스크립트 (ngrok 없음, reload=False, workers=4)

사용법:
  python start_prod.py

환경변수:
  SERVER_HOST   - 바인딩 호스트 (기본: 0.0.0.0)
  SERVER_PORT   - 포트 (기본: 8000)
  SERVER_WORKERS - uvicorn 워커 수 (기본: 4)
"""
import os, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

HOST    = os.getenv("SERVER_HOST", "0.0.0.0")
PORT    = int(os.getenv("SERVER_PORT", "8000"))
WORKERS = int(os.getenv("SERVER_WORKERS", "4"))


def _force_utf8_output() -> None:
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

if __name__ == "__main__":
    _force_utf8_output()
    import uvicorn
    print(f"[prod] 서버 시작: http://{HOST}:{PORT}  workers={WORKERS}")
    uvicorn.run(
        "server.app_server:app",
        host=HOST,
        port=PORT,
        workers=WORKERS,
        reload=False,
        access_log=True,
    )
