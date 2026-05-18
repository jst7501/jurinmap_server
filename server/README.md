# Server Structure

## Core
- `server/core/settings.py`: environment loading and runtime constants (`ROOT_DIR`, Postgres config, Firebase/VAPID config)
- `server/core/security.py`: HTTP/WebSocket access guard (origin/referer allow-list + optional API key)

## DB
- `server/db/connections.py`: PostgreSQL connection entry points (`get_stocks_conn`, `get_news_conn`) and runtime schema guard. Postgres-only.
- `server/db_compat.py`: SQLite-dialect compatibility shim — rewrites `?` placeholders, `INSERT OR IGNORE`, `IFNULL`, `GROUP_CONCAT`, etc. into Postgres syntax so legacy callers keep working without rewrite.

## Services
- `server/services/push_service.py`: push subscription store, FCM token store, Firebase/WebPush delivery helpers

## Legacy Facade
- `server/state.py`: compatibility facade for existing imports; re-exports push helpers from service modules

## Routes
- `server/routes/stocks.py`
- `server/routes/push.py`

## Security Env
- `API_SHARED_KEY`: optional shared key used by key-based guard (`x-api-key`, `Authorization: Bearer ...`, or `api_key` query on WS).
- `API_REQUIRE_HTTP_KEY`: default `true`. Enables API key check for HTTP endpoints.
- `API_REQUIRE_WS_KEY`: default `false`. Set `true` to require API key on WebSocket too.
- `API_ALLOW_ORIGINS`: comma-separated origin allow-list for access guard. Falls back to `CORS_ALLOW_ORIGINS`.
- `API_ALLOW_ORIGIN_REGEX`: optional regex allow-list for dynamic domains. Falls back to `CORS_ALLOW_ORIGIN_REGEX`.
- `API_ALLOW_REFERER_HOSTS`: comma-separated referer host allow-list. If empty, inferred from allowed origins.
- `API_ENFORCE_WEB_ORIGIN`: default `true`. Checks `Origin`/`Referer` and blocks disallowed callers.
- `API_BLOCK_NO_ORIGIN`: default `true`. Blocks requests that have neither `Origin` nor `Referer`.

This is a safe first-step split. Existing imports from `server.state` continue to work.
