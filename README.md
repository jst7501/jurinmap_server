# JURINMAP — Backend

한국 투자정보 대시보드 백엔드. FastAPI + KIS WebSocket + APScheduler 데이터 파이프라인.

프론트엔드(React)는 [별도 repo](https://github.com/jst7501/jurinmap) (Vercel 배포). 이 repo 는 backend 만.

## 빠른 시작 (Docker)

```bash
cp .env.example .env       # KIS_APP_KEY 등 채우기
docker compose up --build -d
# http://localhost:8000
```

상태 확인:
```bash
curl http://localhost:8000/api/ops/metrics
curl http://localhost:8000/api/ops/kis-status
```

종료:
```bash
docker compose down            # 컨테이너만
docker compose down -v         # 볼륨(DB 데이터) 포함 (주의 — 데이터 손실)
```

## 시크릿 설정

`.env` 에 절대 commit 금지 키들:
- `KIS_APP_KEY` / `KIS_APP_SECRET` — 한국투자증권 OpenAPI 키
- `DART_API_KEY` — 공시 데이터 (옵션)
- `FIREBASE_SERVICE_ACCOUNT_KEY` — 푸시 알림 (옵션, JSON 파일 경로)
- `TELEMSG_PUSH_TOKEN` — 외부 푸시 트리거 헤더 (옵션)

Firebase 사용 시:
```bash
mkdir -p secrets
# 다운받은 firebase 서비스 계정 JSON을 아래 경로에 둠
cp ~/Downloads/your-firebase-key.json secrets/firebase-service-account.json
# .env 의 FIREBASE_SERVICE_ACCOUNT_KEY=/secrets/firebase-service-account.json 그대로
```
컨테이너가 `./secrets` 를 `/secrets:ro` 로 마운트.

## 로컬 직접 실행 (Docker 없이)

```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Postgres / Redis 는 별도 실행 (예: brew install postgresql redis)
uvicorn server.app_server:app --reload --host 0.0.0.0 --port 8000
```

## 아키텍처 (요약)

```
KIS OpenAPI  ──┐
KIS WebSocket ─┼─→  FastAPI (server/app_server.py)  ──→  Postgres
DART / 네이버 ─┘                  │                       Redis (캐시)
                                  ▼
                          MCP scheduled-tasks (Claude Code)
                          - 시황 브리핑 5종
                          - 종목 일일 요약
                          - 종토방 민심 분석
                          - 종목 발굴 스크리너 featured
                          - NXT 실시간 통합
```

자세한 설계는 [CLAUDE.md](CLAUDE.md) 참조.

## 디렉토리 맵

| 경로 | 역할 |
|---|---|
| `server/` | FastAPI 라우터 + DB · 모니터링 · 보안 |
| `collectors/` | 외부 API 원천 수집 (KIS · 네이버 · DART) |
| `scrapers/` | HTML 스크래퍼 |
| `references/` | KIS 공식 API 참조 구현 |
| `scripts/` | 일회성 · 정기 스크립트 (cron · APScheduler 진입점) |
| `calculators/` | 기술 지표 계산 |
| `persona/` | AI 인격 · 요약 |
| `config/` | 설정 (대부분 .env 로 분리됨) |

## 운영 가이드

### 첫 셋업 (DB 스키마)

```bash
docker compose up -d postgres
docker compose exec app python scripts/import_to_db.py    # 테이블 ensure + 초기 데이터
```

### KIS 토큰 갱신

KIS Access Token 은 6시간 단위로 만료. 코드가 자동 갱신하지만, 처음 실행 시 `.env` 의 `KIS_APP_KEY/SECRET` 만 있으면 됨.

### 백업

Postgres 데이터는 `pgdata` 도커 볼륨에 저장. 정기 백업 권장:

```bash
docker compose exec postgres pg_dump -U jurinmap jurinmap | gzip > backups/$(date +%Y%m%d).sql.gz
```

### 로그

`./logs/server.log` (호스트 마운트). 컨테이너 stdout 도 `docker compose logs -f app` 으로 가능.

## 배포 (참고)

- **Fly.io NRT** (도쿄): `fly launch` → `fly deploy`. KIS latency 양호.
- **AWS Lightsail 서울**: $5/월 (2vCPU 1GB), 직접 SSH 관리.
- **자체 미니PC / 라즈베리파이**: 24/7 운영. docker compose up 한 번이면 끝.

배포 시:
1. `.env` 의 시크릿은 플랫폼 secret manager 로 관리 (Fly secrets / SSM 등)
2. CORS_ALLOW_ORIGINS 운영 도메인으로 좁힘
3. 리버스 프록시 (nginx · Caddy) 로 HTTPS · gzip
4. dashboard `VITE_API_BASE_URL` 을 배포 URL 로

## 라이선스

내부 프로젝트. 외부 공유 시 KIS · DART · Firebase 시크릿 절대 포함 금지.
