# syntax=docker/dockerfile:1.6
# JURINMAP Backend — FastAPI + KIS WS + APScheduler 데이터 파이프라인

FROM python:3.11-slim AS base

# ─── 환경 변수 (이미지 레벨) ────────────────────────────────
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Seoul \
    LANG=ko_KR.UTF-8 \
    LC_ALL=ko_KR.UTF-8

# ─── OS 의존성 ──────────────────────────────────────────────
# - libpq-dev: psycopg (Postgres)
# - libxml2/libxslt: lxml (네이버 스크래퍼)
# - curl: healthcheck
# - tzdata: Asia/Seoul 타임존
# - build-essential: 일부 패키지 native 빌드 (pandas/yfinance 등은 wheel 있어 보통 불필요하지만 안전)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    libpq-dev \
    libxml2-dev \
    libxslt1-dev \
    curl \
    tzdata \
    locales \
    && sed -i -e 's/# ko_KR.UTF-8 UTF-8/ko_KR.UTF-8 UTF-8/' /etc/locale.gen \
    && locale-gen \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ─── Python 의존성 ──────────────────────────────────────────
# 코드 변경 시 의존성 재설치 회피 (Docker layer caching)
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# ─── 애플리케이션 코드 ──────────────────────────────────────
# .dockerignore 가 시크릿 / 데이터 / 캐시 / dashboard 제외함
COPY . /app

# ─── 비루트 사용자 (보안) ───────────────────────────────────
RUN useradd -m -u 1000 jurin \
    && mkdir -p /app/data /app/logs \
    && chown -R jurin:jurin /app
USER jurin

# ─── 볼륨 — 데이터/로그는 호스트 마운트 권장 ───────────────
VOLUME ["/app/data", "/app/logs"]

# ─── 포트 ───────────────────────────────────────────────────
EXPOSE 8000

# ─── Healthcheck ────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/ops/metrics > /dev/null || exit 1

# ─── 기본 실행 ──────────────────────────────────────────────
# uvicorn 단일 worker (KIS WS 가 in-process 싱글톤 hub 라 worker 1 권장).
# 멀티 worker 가 필요하면 reverse proxy (nginx) 뒤에 두고 hub 충돌 회피 설계 필요.
CMD ["uvicorn", "server.app_server:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
