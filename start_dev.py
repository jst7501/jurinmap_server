"""
start_dev.py  -  개발용 로컬 서버 (포트 8001, --reload)
- cloudflared / 운영 서버(8000) 에 영향 없음
- 코드 수정 시 자동 재시작됨
"""
import os, sys, subprocess

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

if __name__ == "__main__":
    _force_utf8_output()
    print("\n" + "="*60)
    print("  [DEV] 개발 서버 : http://localhost:8000")
    print("  코드 변경 시 자동 재시작 (reload 모드)")
    print("  운영 서버(8000) / cloudflared 영향 없음")
    print("="*60 + "\n")

    subprocess.run(
        [
            sys.executable, "-m", "uvicorn",
            "server.app_server:app",
            "--host", "0.0.0.0",
            "--port", "8001",
            "--reload",
        ],
        cwd=ROOT,
    )
