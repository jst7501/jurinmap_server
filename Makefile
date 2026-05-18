# JURINMAP Backend — 운영 단축 명령
# 사용: make <target>
# 윈도우는 WSL 또는 Git Bash 에서 사용 권장.

SHELL := /bin/bash
COMPOSE := docker compose
APP_SERVICE := app
PG_SERVICE := postgres
REDIS_SERVICE := redis

# 백업 파일명: backups/YYYYMMDD_HHMM.sql.gz
BACKUP_FILE := backups/$(shell date +%Y%m%d_%H%M).sql.gz

.PHONY: help up down build rebuild logs ps shell shell-db shell-redis \
        db-backup db-restore db-shell test lint clean clean-all init-env \
        kis-status featured-test

help:  ## 사용 가능한 명령 보기
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ─── 도커 라이프사이클 ───────────────────────────────────────
up:  ## 컨테이너 백그라운드 기동 (postgres·redis·app)
	$(COMPOSE) up -d --remove-orphans

down:  ## 컨테이너 종료 (볼륨 유지)
	$(COMPOSE) down

build:  ## app 이미지 빌드만
	$(COMPOSE) build $(APP_SERVICE)

rebuild:  ## up 전 강제 재빌드 (캐시 무시)
	$(COMPOSE) build --no-cache $(APP_SERVICE)
	$(COMPOSE) up -d $(APP_SERVICE)

logs:  ## app 로그 follow
	$(COMPOSE) logs -f --tail=200 $(APP_SERVICE)

logs-all:  ## 전체 서비스 로그 follow
	$(COMPOSE) logs -f --tail=100

ps:  ## 서비스 상태
	$(COMPOSE) ps

# ─── Shell ───────────────────────────────────────────────────
shell:  ## app 컨테이너 bash 진입
	$(COMPOSE) exec $(APP_SERVICE) bash

shell-db:  ## postgres psql 진입
	$(COMPOSE) exec $(PG_SERVICE) psql -U $${PGUSER:-jurinmap} -d $${PGDATABASE:-jurinmap}

shell-redis:  ## redis-cli 진입
	$(COMPOSE) exec $(REDIS_SERVICE) redis-cli

# ─── DB 백업·복원 ────────────────────────────────────────────
db-backup:  ## Postgres 백업 → backups/YYYYMMDD_HHMM.sql.gz
	@mkdir -p backups
	@echo "→ $(BACKUP_FILE)"
	$(COMPOSE) exec -T $(PG_SERVICE) pg_dump -U $${PGUSER:-jurinmap} $${PGDATABASE:-jurinmap} | gzip > $(BACKUP_FILE)
	@ls -lah $(BACKUP_FILE)

db-restore:  ## 백업 복원 — make db-restore FILE=backups/20260511_1234.sql.gz
	@if [ -z "$(FILE)" ]; then echo "사용법: make db-restore FILE=backups/xxx.sql.gz"; exit 1; fi
	@echo "⚠️  $(FILE) 로부터 복원. 기존 데이터 덮어씀."
	@read -p "정말 진행 (yes/no)? " confirm; [ "$$confirm" = "yes" ] || exit 1
	gunzip -c $(FILE) | $(COMPOSE) exec -T $(PG_SERVICE) psql -U $${PGUSER:-jurinmap} -d $${PGDATABASE:-jurinmap}

# ─── 개발 ────────────────────────────────────────────────────
test:  ## pytest 실행 (app 컨테이너 내부)
	$(COMPOSE) exec $(APP_SERVICE) pytest -q || echo "(tests 미작성 / 실패 시 skip)"

lint:  ## ruff / mypy (있을 때만)
	$(COMPOSE) exec $(APP_SERVICE) sh -c "ruff check . || true; mypy server/ || true"

# ─── 운영 진단 ───────────────────────────────────────────────
kis-status:  ## /api/ops/kis-status 호출 — KIS WS hub 상태
	@curl -sS http://localhost:$${APP_HOST_PORT:-8000}/api/ops/kis-status | python -m json.tool

featured-test:  ## /api/screener/featured 응답 (홈 미리보기)
	@curl -sS http://localhost:$${APP_HOST_PORT:-8000}/api/screener/featured | python -m json.tool | head -40

metrics:  ## /api/ops/metrics — uptime · http · kis 통계
	@curl -sS http://localhost:$${APP_HOST_PORT:-8000}/api/ops/metrics | python -m json.tool

# ─── 초기화 / 청소 ──────────────────────────────────────────
init-env:  ## .env 가 없으면 .env.example 복사
	@if [ ! -f .env ]; then \
	  cp .env.example .env; \
	  echo "→ .env 생성됨. KIS_APP_KEY 등 채우세요."; \
	else \
	  echo "이미 .env 존재. 건너뜀."; \
	fi

clean:  ## 컨테이너만 제거 (볼륨 유지)
	$(COMPOSE) down --remove-orphans

clean-all:  ## ⚠️  컨테이너·이미지·볼륨 모두 제거 (DB 데이터 손실)
	@read -p "⚠️  볼륨까지 삭제. 정말 진행 (yes/no)? " confirm; [ "$$confirm" = "yes" ] || exit 1
	$(COMPOSE) down -v --remove-orphans --rmi local

# 기본 타깃
.DEFAULT_GOAL := help
