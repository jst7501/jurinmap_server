"""ai_trader — Claude Opus 가상 매매 에이전트 도구 모듈.

import 시 stdout/stderr 를 UTF-8 로 reconfigure 해서
Windows cp949 콘솔에서 한글·em-dash 인코딩 에러 방지.
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
