# """
# start_all.py
# uvicorn HTTP 서버 시작 (포트 8000) - crash 시 자동 재시작

# cloudflared는 별도로 실행:
#   cloudflared tunnel --config C:\Users\jst75\.cloudflared\config.yml run
# """

import os, sys, subprocess, time, logging

ROOT = os.path.dirname(os.path.abspath(__file__))


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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("start_all")


# ── uvicorn 자동 재시작 루프 ───────────────────────────────────
def start_server():
    os.chdir(ROOT)
    while True:
        logger.info("[uvicorn] 서버 시작 (포트 8000)...")
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn",
                "server.app_server:app",
                "--host", "0.0.0.0",
                "--port", "8000",
                "--http", "h11",  # HTTP/1.1 강제 → Cloudflare 터널 WebSocket 호환
            ],
            cwd=ROOT,
        )
        exit_code = proc.wait()
        if exit_code == 0:
            logger.info("[uvicorn] 정상 종료.")
            break
        logger.warning("[uvicorn] 비정상 종료 (exit=%d), 3초 후 재시작...", exit_code)
        time.sleep(3)


# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _force_utf8_output()
    print(f"\n{'='*60}")
    print(f"  로컬 서버: http://localhost:8000")
    print(f"  cloudflared는 별도 터미널에서 실행하세요")
    print(f"  cloudflared tunnel --config C:\\Users\\jst75\\.cloudflared\\config.yml run")
    print(f"{'='*60}\n")

    start_server()
