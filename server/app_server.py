"""
FastAPI 백엔드 서버 (thin entry point)
Postgres 기반 API 서버
"""
import sys, os, threading, time, logging, subprocess, json, hmac
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from server.core.settings import ROOT_DIR, DATA_DIR, JSON_LATEST
from server.core.encoding import force_utf8_runtime
from server.core.security import _env_bool, security_runtime_summary, verify_http_request
from server.routes.stocks import (
    router as stocks_router,
    start_timeline_background_poller,
    start_limit_up_break_background_poller,
    start_hot_rankings_background_poller,
    start_price_refresh_background_poller,
    start_news_background_poller,
    start_macro_background_poller,
    start_etf_background_poller,
    start_home_snapshot_background_refresher,
    run_redis_prewarm,
)
from server.routes.push import router as push_router
from server.routes.watchlist import router as watchlist_router
from server.routes.feedback import router as feedback_router
from server.routes.disclosures import router as disclosures_router
from server.routes.community import router as community_router
from server.routes.trades import router as trades_router
from server.routes.ipo import router as ipo_router
from server.routes.patches import router as patches_router
from server.routes.global_indicators import router as global_indicators_router
from server.routes.votes import router as votes_router
from server.routes.market_brief import router as market_brief_router
from server.routes.nxt import router as nxt_router
from server.routes.screener import router as screener_router
from server.routes.value_chain import router as value_chain_router
from server.routes.ai_trader import router as ai_trader_router
from server.routes.credit_short import router as credit_short_router
from server.routes.data_health import router as data_health_router
from server.routes.events import router as events_router
from server.routes.big_news import router as big_news_router
# from server.routes.briefing import router as briefing_router  # 중단
from server.monitoring import snapshot as monitoring_snapshot, observe_http_request, is_kis_degraded


force_utf8_runtime()

# ??? 濡쒓퉭 ?ㅼ젙 ????????????????????????????????????????????????
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            os.path.join(ROOT_DIR, "server.log"),
            maxBytes=50 * 1024 * 1024,  # 50MB
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("app_server")

_NIGHTLY_LOCK = threading.Lock()
_NIGHTLY_STARTED = False
_NIGHTLY_LAST_RUN_DATE = ""
_NIGHTLY_STATUS: dict = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_reason": None,
    "last_ok": None,
    "steps": [],
    "error": None,
}

_MORNING_LOCK = threading.Lock()
_MORNING_STARTED = False
_MORNING_LAST_RUN_DATE = ""
_MORNING_STATUS: dict = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_ok": None,
    "steps": [],
    "error": None,
}


def _patch_kis_collector_metrics_once():
    try:
        from collectors.kis_api import KISCollector
        from server.monitoring import observe_kis_call
    except Exception as e:
        logger.warning("KIS monitoring patch import failed: %s", e)
        return

    if getattr(KISCollector, "_monitoring_patched", False):
        return

    original_get = KISCollector._get

    def _wrapped_get(self, path, params, tr_id):
        started = time.time()
        try:
            res = original_get(self, path, params, tr_id)
            ok = isinstance(res, dict) and str(res.get("rt_cd")) == "0"
            observe_kis_call(ok, time.time() - started)
            return res
        except Exception:
            observe_kis_call(False, time.time() - started)
            raise

    KISCollector._get = _wrapped_get
    KISCollector._monitoring_patched = True
    logger.info("KIS collector monitoring patch enabled")

_TRANSPARENT_PIXEL_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff"
    b"\xff?\x00\x05\xfe\x02\xfeA\xe2%\x9b\x00\x00\x00\x00IEND\xaeB`\x82"
)


class LogoFallbackStaticFiles(StaticFiles):
    """Return a transparent PNG for missing company logo files.

    Also sets `Cache-Control: public, max-age=86400, immutable` on logo responses
    so browsers don't revalidate (304 round-trips) on every page load.
    """

    # 1일 캐시 — 종목 로고는 거의 바뀌지 않음. immutable은 revalidation 억제.
    _LOGO_CACHE_CONTROL = "public, max-age=86400, immutable"

    @staticmethod
    def _is_company_logo_path(path: str) -> bool:
        normalized = (path or "").replace("\\", "/").lstrip("/")
        return normalized.startswith("company_logos/")

    def _apply_cache_headers(self, response: Response, path: str) -> Response:
        if self._is_company_logo_path(path):
            response.headers["Cache-Control"] = self._LOGO_CACHE_CONTROL
        return response

    async def get_response(self, path: str, scope):
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and self._is_company_logo_path(path):
                fallback = Response(content=_TRANSPARENT_PIXEL_PNG, media_type="image/png", status_code=200)
                return self._apply_cache_headers(fallback, path)
            raise

        if response.status_code == 404 and self._is_company_logo_path(path):
            fallback = Response(content=_TRANSPARENT_PIXEL_PNG, media_type="image/png", status_code=200)
            return self._apply_cache_headers(fallback, path)
        return self._apply_cache_headers(response, path)


def _parse_cors_allow_origins() -> list[str]:
    if os.getenv("CORS_OPEN", "true").strip().lower() in {"1", "true", "yes", "on"}:
        return ["*"]
    raw = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
    if raw:
        return [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]
    return [
        "https://jurinmap.com",
        "https://www.jurinmap.com",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]


def _run_subprocess_step(name: str, cmd: list[str], timeout_sec: int = 7200) -> dict:
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=max(60, int(timeout_sec)),
        )
        out_lines = (proc.stdout or "").strip().splitlines()
        err_lines = (proc.stderr or "").strip().splitlines()
        return {
            "name": name,
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "stdout_tail": out_lines[-20:],
            "stderr_tail": err_lines[-20:],
        }
    except Exception as e:
        return {
            "name": name,
            "ok": False,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "error": str(e),
        }


def _run_nightly_maintenance(reason: str):
    if not _NIGHTLY_LOCK.acquire(blocking=False):
        return
    try:
        _NIGHTLY_STATUS["running"] = True
        _NIGHTLY_STATUS["last_reason"] = reason
        _NIGHTLY_STATUS["last_started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _NIGHTLY_STATUS["error"] = None
        _NIGHTLY_STATUS["steps"] = []
        started = time.time()

        refresh_sleep = os.getenv("NIGHTLY_REFRESH_SLEEP", "0.20").strip() or "0.20"
        refresh_batch = os.getenv("NIGHTLY_REFRESH_BATCH", "50").strip() or "50"
        refresh_limit = (os.getenv("NIGHTLY_REFRESH_LIMIT", "").strip() or "")
        cmd = [
            sys.executable,
            os.path.join(ROOT_DIR, "scripts", "nightly_reliability_refresh.py"),
            "--sleep",
            refresh_sleep,
            "--batch",
            refresh_batch,
        ]
        if refresh_limit:
            cmd.extend(["--limit", refresh_limit])

        _NIGHTLY_STATUS["steps"].append(_run_subprocess_step("nightly_reliability_refresh", cmd))

        prewarm_t0 = time.time()
        try:
            prewarm = run_redis_prewarm()
            _NIGHTLY_STATUS["steps"].append(
                {
                    "name": "redis_prewarm",
                    "ok": bool(prewarm.get("ok")),
                    "elapsed_ms": int((time.time() - prewarm_t0) * 1000),
                    "detail": prewarm,
                }
            )
        except Exception as e:
            _NIGHTLY_STATUS["steps"].append(
                {
                    "name": "redis_prewarm",
                    "ok": False,
                    "elapsed_ms": int((time.time() - prewarm_t0) * 1000),
                    "error": str(e),
                }
            )

        ok = all(bool(step.get("ok")) for step in _NIGHTLY_STATUS["steps"])
        _NIGHTLY_STATUS["last_ok"] = ok
        _NIGHTLY_STATUS["last_finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _NIGHTLY_STATUS["elapsed_ms"] = int((time.time() - started) * 1000)
    except Exception as e:
        _NIGHTLY_STATUS["last_ok"] = False
        _NIGHTLY_STATUS["error"] = str(e)
        _NIGHTLY_STATUS["last_finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    finally:
        _NIGHTLY_STATUS["running"] = False
        _NIGHTLY_LOCK.release()


def trigger_nightly_maintenance(reason: str = "manual") -> bool:
    if _NIGHTLY_STATUS.get("running"):
        return False
    t = threading.Thread(target=_run_nightly_maintenance, args=(reason,), daemon=True, name="nightly-maintenance")
    t.start()
    return True


def _run_morning_maintenance(reason: str):
    """아침 루틴: macro 캐시 무효화. KST 07:05에 발화.
    미국 지수·테마 수집 step (fetch_us_indices / refresh_us_themes) 은
    pennymap-backend 이관 (2026-05-29).
    """
    if not _MORNING_LOCK.acquire(blocking=False):
        return
    try:
        _MORNING_STATUS["running"] = True
        _MORNING_STATUS["last_reason"] = reason
        _MORNING_STATUS["last_started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _MORNING_STATUS["error"] = None
        _MORNING_STATUS["steps"] = []
        started = time.time()

        # macro 캐시 무효화 (Redis)
        redis_t0 = time.time()
        try:
            from server.cache import _get_client, _prefixed
            r = _get_client()
            if r is not None:
                n = 0
                for pat in ("stocks:macro:*", "stocks:themes_us:*"):
                    for k in list(r.scan_iter(match=_prefixed(pat), count=100)):
                        ks = k.decode() if isinstance(k, bytes) else k
                        r.delete(ks)
                        n += 1
                _MORNING_STATUS["steps"].append(
                    {
                        "name": "purge_cache",
                        "ok": True,
                        "elapsed_ms": int((time.time() - redis_t0) * 1000),
                        "detail": {"deleted_keys": n},
                    }
                )
            else:
                _MORNING_STATUS["steps"].append(
                    {"name": "purge_cache", "ok": True, "detail": {"redis": "disabled"}}
                )
        except Exception as e:
            _MORNING_STATUS["steps"].append(
                {"name": "purge_cache", "ok": False, "error": str(e)}
            )

        ok = all(bool(s.get("ok")) for s in _MORNING_STATUS["steps"])
        _MORNING_STATUS["last_ok"] = ok
        _MORNING_STATUS["last_finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _MORNING_STATUS["elapsed_ms"] = int((time.time() - started) * 1000)
    except Exception as e:
        _MORNING_STATUS["last_ok"] = False
        _MORNING_STATUS["error"] = str(e)
        _MORNING_STATUS["last_finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    finally:
        _MORNING_STATUS["running"] = False
        _MORNING_LOCK.release()


def trigger_morning_maintenance(reason: str = "manual") -> bool:
    if _MORNING_STATUS.get("running"):
        return False
    t = threading.Thread(target=_run_morning_maintenance, args=(reason,), daemon=True, name="morning-maintenance")
    t.start()
    return True


def _start_morning_scheduler():
    global _MORNING_STARTED, _MORNING_LAST_RUN_DATE
    if _MORNING_STARTED:
        return
    _MORNING_STARTED = True
    run_hour = int(os.getenv("MORNING_BATCH_HOUR", "7"))
    run_minute = int(os.getenv("MORNING_BATCH_MINUTE", "5"))

    def _loop():
        nonlocal run_hour, run_minute
        global _MORNING_LAST_RUN_DATE
        while True:
            try:
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                # 오늘 해당 시각 이후이면서 아직 오늘 실행 안 했으면 발화
                due = (now.hour > run_hour) or (now.hour == run_hour and now.minute >= run_minute)
                # 하지만 하루 종일이 due가 되지 않도록 run 시각 이후 3시간 윈도우만
                within_window = now.hour < (run_hour + 3)
                if due and within_window and _MORNING_LAST_RUN_DATE != today and (not _MORNING_STATUS.get("running")):
                    if trigger_morning_maintenance(reason="scheduler"):
                        _MORNING_LAST_RUN_DATE = today
            except Exception as e:
                logger.debug("morning scheduler error: %s", e)
            time.sleep(45)

    t = threading.Thread(target=_loop, daemon=True, name="morning-scheduler")
    t.start()
    logger.info("morning scheduler started at %02d:%02d", run_hour, run_minute)


def _start_nightly_scheduler():
    global _NIGHTLY_STARTED, _NIGHTLY_LAST_RUN_DATE
    if _NIGHTLY_STARTED:
        return
    _NIGHTLY_STARTED = True
    run_hour = int(os.getenv("NIGHTLY_BATCH_HOUR", "18"))
    run_minute = int(os.getenv("NIGHTLY_BATCH_MINUTE", "10"))

    def _loop():
        nonlocal run_hour, run_minute
        global _NIGHTLY_LAST_RUN_DATE
        while True:
            try:
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                due = (now.hour > run_hour) or (now.hour == run_hour and now.minute >= run_minute)
                if due and _NIGHTLY_LAST_RUN_DATE != today and (not _NIGHTLY_STATUS.get("running")):
                    if trigger_nightly_maintenance(reason="scheduler"):
                        _NIGHTLY_LAST_RUN_DATE = today
            except Exception as e:
                logger.debug("nightly scheduler error: %s", e)
            time.sleep(45)

    t = threading.Thread(target=_loop, daemon=True, name="nightly-scheduler")
    t.start()
    logger.info("nightly scheduler started at %02d:%02d", run_hour, run_minute)


@asynccontextmanager
def _env_truthy(name: str, default: str = "") -> bool:
    """환경변수 true/false 해석 — '1','true','yes','on' 은 모두 True."""
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


async def _lifespan(app: FastAPI):
    """서버 lifespan — 무거운 백그라운드 폴러를 env 로 제어.

    SERVER_LIGHT_STARTUP=1 이면 네트워크 크롤링 폴러(ETF 1,092·가격 2,691·
    KIS 타임라인·홈 스냅샷)를 건너뛴다. 개발 중 잦은 재시작을 가볍게 하려는 용도.
    경량 폴러(뉴스·매크로·랭킹)와 크론 스케줄러는 그대로 유지돼 API 응답은 정상.

    개별 끄기:
      SKIP_ETF_POLLER=1 / SKIP_PRICE_REFRESH=1 / SKIP_HOME_SNAPSHOT=1
      SKIP_TIMELINE_POLLER=1 / SKIP_LIMIT_UP_POLLER=1
    """
    logger.info("server start")
    _patch_kis_collector_metrics_once()

    # ── Windows ProactorEventLoop 무해 노이즈 억제 ─────────────────────────
    # 클라이언트(cloudflared 경유) 연결이 쓰기 도중 끊기면 소켓 transport 의
    # _ProactorBaseWritePipeTransport._loop_writing 콜백에서
    #   AssertionError: assert f is self._write_fut
    # 가 터져 로그를 도배한다. CPython 의 알려진 Windows 버그로 데이터 손상은 없고
    # 죽는 연결 1개에 국한된다. 그 케이스(=_loop_writing 의 AssertionError)만 조용히 무시.
    try:
        import sys as _sys
        if _sys.platform == "win32":
            import asyncio as _asyncio

            _loop = _asyncio.get_running_loop()
            _prev_handler = _loop.get_exception_handler()

            def _proactor_noise_filter(loop, context):
                msg = context.get("message", "") or ""
                exc = context.get("exception")
                if "_loop_writing" in msg and isinstance(exc, AssertionError):
                    return  # 무해한 Proactor write-future race — 로그 생략
                if _prev_handler:
                    _prev_handler(loop, context)
                else:
                    loop.default_exception_handler(context)

            _loop.set_exception_handler(_proactor_noise_filter)
            logger.info("[startup] Proactor _loop_writing AssertionError 노이즈 필터 설치")
    except Exception as _e:
        logger.debug("proactor noise filter skip: %s", _e)
    try:
        from server.state import ensure_hot_query_indexes
        ensure_hot_query_indexes()
    except Exception as e:
        logger.debug("ensure_hot_query_indexes skipped: %s", e)

    light = _env_truthy("SERVER_LIGHT_STARTUP")
    if light:
        logger.info("[startup] SERVER_LIGHT_STARTUP=1 — heavy pollers skipped")

    # ── 무거운 폴러(크롤링·벌크 리프레시)는 개별 gate 로 끌 수 있게 ──
    if not (light or _env_truthy("SKIP_TIMELINE_POLLER")):
        start_timeline_background_poller()
    if not (light or _env_truthy("SKIP_LIMIT_UP_POLLER")):
        start_limit_up_break_background_poller()
    if not (light or _env_truthy("SKIP_PRICE_REFRESH")):
        start_price_refresh_background_poller()
    if not (light or _env_truthy("SKIP_ETF_POLLER")):
        start_etf_background_poller()
    if not (light or _env_truthy("SKIP_HOME_SNAPSHOT")):
        start_home_snapshot_background_refresher()

    # ── 경량 폴러·랭킹·매크로·뉴스 — 기본 항상 실행 (응답 가벼움) ──
    start_hot_rankings_background_poller()
    start_news_background_poller()
    start_macro_background_poller()

    # ── 크론 스케줄러는 타이머라 부담 없음, 항상 실행 ──
    _start_nightly_scheduler()
    _start_morning_scheduler()
    try:
        from server.data_pipeline_scheduler import start_data_pipeline_scheduler
        start_data_pipeline_scheduler()
    except Exception as e:
        logger.warning("data_pipeline_scheduler failed to start: %s", e)

    yield
    logger.info("server stop")




import datetime as _dt_mod
from decimal import Decimal as _Decimal
from fastapi.responses import JSONResponse as _StdJSONResponse


class SafeJSONResponse(_StdJSONResponse):
    def render(self, content) -> bytes:
        def _default(o):
            if isinstance(o, _dt_mod.datetime):
                return o.isoformat(sep=" ")
            if isinstance(o, _dt_mod.date):
                return o.isoformat()
            if isinstance(o, _Decimal):
                return float(o)
            if isinstance(o, (bytes, bytearray)):
                try:
                    return o.decode("utf-8")
                except Exception:
                    return o.hex()
            return str(o)

        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
            default=_default,
        ).encode("utf-8")


_API_DOCS_ENABLED = _env_bool("ENABLE_API_DOCS", False)

app = FastAPI(title="二쇱떇 ??쒕낫??API", version="2.0", lifespan=_lifespan, default_response_class=SafeJSONResponse,
              docs_url="/docs" if _API_DOCS_ENABLED else None,
              redoc_url=None,
              openapi_url="/openapi.json" if _API_DOCS_ENABLED else None)
os.makedirs(os.path.join(DATA_DIR, "company_logos"), exist_ok=True)
app.mount("/assets", LogoFallbackStaticFiles(directory=DATA_DIR), name="assets")
app.mount("/api/assets", LogoFallbackStaticFiles(directory=DATA_DIR), name="api-assets")

# ??? GZip ?뺤텞 (1KB ?댁긽 ?묐떟 ?먮룞 ?뺤텞) ?????????????????????
app.add_middleware(GZipMiddleware, minimum_size=1000)

cors_allow_origins = _parse_cors_allow_origins()
cors_allow_origin_regex = os.getenv("CORS_ALLOW_ORIGIN_REGEX", "").strip() or None
logger.info("CORS allow_origins=%s, allow_origin_regex=%s", cors_allow_origins, cors_allow_origin_regex)
logger.info("API security guard=%s", security_runtime_summary())

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allow_origins,
    allow_origin_regex=cors_allow_origin_regex,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
    max_age=3600,
)


# ??? 寃쎈줈蹂?Cache-Control TTL 留ㅽ븨 ??????????????????????????
_CACHE_TTL: list[tuple[str, int]] = [
    ("/api/home/snapshot",    10),
    ("/api/stocks/ranking/",  15),   # ?ㅼ떆媛??쒖쐞
    ("/api/stocks/",           3),   # 媛쒕퀎 醫낅ぉ (price/orderbook ?ы븿)
    ("/api/stocks",           30),   # 醫낅ぉ 紐⑸줉
    ("/api/indices",          30),
    ("/api/market-status",    30),
    ("/api/market-signal",    60),
    ("/api/market-brief",     60),
    ("/api/themes",          120),
    ("/api/macro",           120),
    # /api/news 캐시 제거 (2026-05-19) — startswith 매치라 /api/news/{id} 상세도 같이 잡혀서
    # 푸시 클릭 시 새 뉴스가 stale 응답을 받는 문제 발생. 뉴스는 신선도 우선.
    ("/api/vi-status",        30),
]

def _get_cache_ttl(path: str) -> int | None:
    for prefix, ttl in _CACHE_TTL:
        if path.startswith(prefix):
            return ttl
    return None


_DEGRADED_META_PREFIXES: tuple[str, ...] = (
    "/api/home/",
    "/api/stocks",
    "/api/news",
    "/api/macro",
    "/api/market-",
    "/api/vi-status",
    "/api/timeline",
)
_DEGRADED_META_BODY_ENABLED = str(os.getenv("DEGRADED_META_BODY_ENABLED", "1")).strip().lower() in ("1", "true", "yes", "on")


def _is_degraded_meta_target(path: str) -> bool:
    p = str(path or "").strip()
    return any(p.startswith(prefix) for prefix in _DEGRADED_META_PREFIXES)


def _attach_degraded_meta_to_response(
    request: Request,
    response,
    kis_degraded: bool,
):
    if not request.url.path.startswith("/api/"):
        return response

    response.headers["X-KIS-Degraded"] = "1" if kis_degraded else "0"
    if kis_degraded and request.method.upper() == "GET":
        response.headers["X-Data-Stale"] = "1"

    if not _DEGRADED_META_BODY_ENABLED:
        return response
    if not _is_degraded_meta_target(request.url.path):
        return response
    if "application/json" not in str(response.headers.get("content-type") or "").lower():
        return response

    body = getattr(response, "body", None)
    if body is None:
        return response
    try:
        payload = json.loads(body)
    except Exception:
        return response
    if not isinstance(payload, dict):
        return response

    changed = False
    degraded_meta = dict(payload.get("_degraded") or {})
    if degraded_meta.get("kis_auto") != bool(kis_degraded):
        degraded_meta["kis_auto"] = bool(kis_degraded)
        payload["_degraded"] = degraded_meta
        changed = True

    if kis_degraded and request.method.upper() == "GET" and payload.get("_stale") is not True:
        payload["_stale"] = True
        changed = True

    if not changed:
        return response

    try:
        new_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response.body = new_body
        response.headers["Content-Length"] = str(len(new_body))
    except Exception:
        return response
    return response

# ??? ?붿껌 濡쒓퉭 + Cache-Control 誘몃뱾?⑥뼱 ??????????????????????
_ADMIN_PROXY_HEADERS = ("x-forwarded-for", "cf-connecting-ip")


def _is_admin_request_allowed(request: Request) -> bool:
    token = (os.getenv("ADMIN_API_TOKEN") or "").strip()
    # compare_digest 는 비ASCII str 에 TypeError — 헤더는 latin-1 디코딩이라 bytes 로 비교
    if token and hmac.compare_digest(
        request.headers.get("x-admin-token", "").encode(), token.encode()
    ):
        return True
    # 로컬 직접 호출 허용 — Cloudflare/nginx 경유 트래픽은 x-forwarded-for /
    # cf-connecting-ip 를 항상 붙이므로, 해당 헤더가 있으면 외부로 간주.
    client_host = request.client.host if request.client else None
    if client_host in ("127.0.0.1", "::1") and not any(
        request.headers.get(h) for h in _ADMIN_PROXY_HEADERS
    ):
        return True
    return False


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    path = request.url.path
    if path.startswith("/api/admin") or path == "/api/ops/metrics":
        if not _is_admin_request_allowed(request):
            elapsed = time.time() - start
            logger.warning(
                "ADMIN BLOCK %s %s client=%s %.3fs",
                request.method,
                path,
                request.client.host if request.client else None,
                elapsed,
            )
            observe_http_request(403, elapsed)
            return JSONResponse(status_code=403, content={"detail": "admin_forbidden"})

    allowed, reason = verify_http_request(request)
    if not allowed:
        elapsed = time.time() - start
        logger.warning(
            "BLOCK %s %s reason=%s %.3fs",
            request.method,
            request.url.path,
            reason or "forbidden",
            elapsed,
        )
        observe_http_request(403, elapsed)
        return JSONResponse(
            status_code=403,
            content={"detail": "forbidden", "reason": reason or "forbidden"},
        )

    response = await call_next(request)
    elapsed = time.time() - start
    observe_http_request(int(response.status_code), elapsed)
    logger.info("%s %s %d %.3fs", request.method, request.url.path, response.status_code, elapsed)

    kis_degraded = False
    try:
        if request.url.path.startswith("/api/"):
            kis_degraded = bool(is_kis_degraded())
    except Exception:
        kis_degraded = False

    # GET 200 ?묐떟?먮쭔 Cache-Control 二쇱엯 ??Cloudflare ?ｌ? 罹먯떛
    if request.method == "GET" and response.status_code == 200:
        ttl = _get_cache_ttl(request.url.path)
        if ttl:
            response.headers["Cache-Control"] = f"public, max-age={ttl}, stale-while-revalidate={ttl * 2}"
        elif request.url.path.startswith("/api/"):
            # 2026-06-02 — ttl 없는 /api/* 는 명시 no-store.
            # 5/29 사고: Nginx fallback (백엔드 502→index.html) 200 응답을 CDN 이 4일째 캐시.
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["CDN-Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"

    response = _attach_degraded_meta_to_response(request, response, kis_degraded)

    return response


app.include_router(stocks_router)
app.include_router(push_router)
app.include_router(watchlist_router)
app.include_router(feedback_router)
app.include_router(disclosures_router)
app.include_router(community_router)
app.include_router(trades_router)
app.include_router(ipo_router)  # routers registered
app.include_router(patches_router)
app.include_router(global_indicators_router)
app.include_router(votes_router)
app.include_router(market_brief_router)
app.include_router(nxt_router)
app.include_router(screener_router)
app.include_router(value_chain_router)
app.include_router(ai_trader_router)
app.include_router(credit_short_router)
app.include_router(data_health_router)
app.include_router(events_router)
app.include_router(big_news_router)


@app.get("/api/ops/metrics")
def get_ops_metrics(window_sec: int = 300):
    out = monitoring_snapshot(window_sec=window_sec)
    try:
        out["degraded"] = {
            "kis_auto": bool(is_kis_degraded(window_sec=window_sec)),
        }
    except Exception:
        out["degraded"] = {"kis_auto": False}
    return out


@app.get("/api/admin/nightly/status")
def get_nightly_status():
    return {
        **_NIGHTLY_STATUS,
        "schedule_hour": int(os.getenv("NIGHTLY_BATCH_HOUR", "18")),
        "schedule_minute": int(os.getenv("NIGHTLY_BATCH_MINUTE", "10")),
    }


@app.post("/api/admin/nightly/run")
def run_nightly_now():
    started = trigger_nightly_maintenance(reason="manual")
    return {"started": bool(started), "running": bool(_NIGHTLY_STATUS.get("running"))}


@app.get("/api/admin/morning/status")
def get_morning_status():
    return {
        **_MORNING_STATUS,
        "schedule_hour": int(os.getenv("MORNING_BATCH_HOUR", "7")),
        "schedule_minute": int(os.getenv("MORNING_BATCH_MINUTE", "5")),
    }


@app.post("/api/admin/morning/run")
def run_morning_now():
    started = trigger_morning_maintenance(reason="manual")
    return {"started": bool(started), "running": bool(_MORNING_STATUS.get("running"))}


# ── 데이터 파이프라인 스케줄러 상태/수동 트리거 ─────────────────────────
@app.get("/api/admin/pipeline/status")
def get_pipeline_status():
    try:
        from server.data_pipeline_scheduler import scheduler_status
        return scheduler_status()
    except Exception as e:
        return {"enabled": False, "error": str(e), "jobs": []}


@app.post("/api/admin/stock_detail/flush")
def flush_stock_detail_cache():
    """종목 상세 응답 캐시(mem + Redis)를 모두 비운다.
    라우트/스키마 변경 후 즉시 반영이 필요할 때 사용."""
    mem_n = 0
    redis_n = 0
    try:
        from server.routes.stocks_parts.part03_search_news_live import (
            _STOCK_DETAIL_MEM_CACHE, _STOCK_DETAIL_MEM_LOCK,
        )
        with _STOCK_DETAIL_MEM_LOCK:
            mem_n = len(_STOCK_DETAIL_MEM_CACHE)
            _STOCK_DETAIL_MEM_CACHE.clear()
    except Exception as e:
        return {"ok": False, "error": f"mem flush fail: {e}"}
    try:
        import redis as _redis
        r = _redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"))
        keys = list(r.scan_iter(match="stocks:detail:*", count=1000))
        if keys:
            r.delete(*keys)
            redis_n = len(keys)
    except Exception as e:
        return {"ok": True, "flushed_mem": mem_n, "flushed_redis": 0, "redis_error": str(e)}
    return {"ok": True, "flushed_mem": mem_n, "flushed_redis": redis_n}


@app.post("/api/admin/pipeline/run/{job_id}")
def run_pipeline_job_now(job_id: str):
    try:
        from server.data_pipeline_scheduler import trigger_job_now
        ok = trigger_job_now(job_id)
        return {"triggered": bool(ok), "job_id": job_id}
    except Exception as e:
        return {"triggered": False, "job_id": job_id, "error": str(e)}


@app.post("/api/admin/pipeline/remove/{job_id}")
def remove_pipeline_job(job_id: str):
    """실시간 스케줄 잡 제거 (재시작 없이 즉시 비활성화).
    영구 제거가 아니라 이번 프로세스 lifetime 한정. 서버 재시작 시 env 설정을
    따라 다시 붙느냐 마느냐 결정된다."""
    try:
        from server.data_pipeline_scheduler import remove_job
        ok = remove_job(job_id)
        return {"removed": bool(ok), "job_id": job_id}
    except Exception as e:
        return {"removed": False, "job_id": job_id, "error": str(e)}


# ??? POST /api/refresh ????????????????????????????????????????
@app.post("/api/refresh")
def refresh_db(background_tasks: BackgroundTasks):
    if not os.path.exists(JSON_LATEST):
        from fastapi import HTTPException
        raise HTTPException(404, "top100_full_latest.json ?놁쓬")

    def _run():
        import subprocess
        script = os.path.join(ROOT_DIR, "scripts", "import_to_db.py")
        subprocess.run([sys.executable, script], cwd=ROOT_DIR, check=True)
        logger.info("refresh: import_to_db.py finished")

    background_tasks.add_task(_run)
    return {"status": "ok", "message": "DB ?щ줈???쒖옉??(諛깃렇?쇱슫??"}


# ??? ?ㅽ뻾 ?????????????????????????????????????????????????????
# AI summary read endpoints (2026-04-21)
# 2026-04-22 JSON 직렬화 안전화: psycopg가 timestamp 컬럼을 datetime 객체로 반환 →
# FastAPI 기본 JSONResponse 직렬화 실패. _jsonable() 로 문자열·원시형만 남김.

def _jsonable(v):
    """datetime / date / Decimal 등 JSON 비지원 타입을 문자열로 강제."""
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat(sep=" ")
        except TypeError:
            return v.isoformat()
    # psycopg Decimal / Numeric 등
    if hasattr(v, "__float__") and not isinstance(v, (int, float, bool)):
        try:
            return float(v)
        except Exception:
            return str(v)
    return v


def _brief_row_to_item(row):
    import json as _json
    ctx = None
    if row[6]:
        try:
            ctx = _json.loads(row[6]) if isinstance(row[6], str) else row[6]
        except Exception:
            ctx = None
    return {
        "id": row[0],
        "market": row[1],
        "slot": row[2],
        "briefing_date": row[3],
        "slot_time": row[4],
        "summary": row[5] or "",
        "context": ctx,
        "model": row[7] or "",
        "created_at": row[8],
    }


@app.get("/api/briefings/latest")
def briefings_latest():
    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    out = {"KOSPI": None, "NASDAQ": None}
    try:
        for market in ("KOSPI", "NASDAQ"):
            row = conn.execute(
                "SELECT id, market, slot, briefing_date, slot_time, summary, context_json, model, created_at "
                "FROM market_briefings WHERE market=%s "
                "ORDER BY briefing_date DESC, slot_time DESC, id DESC LIMIT 1",
                (market,),
            ).fetchone()
            if row:
                out[market] = _brief_row_to_item(row)
    finally:
        conn.close()
    return out


@app.get("/api/briefings")
def briefings_list(market: str | None = None, limit: int = 20):
    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        if market:
            rows = conn.execute(
                "SELECT id, market, slot, briefing_date, slot_time, summary, context_json, model, created_at "
                "FROM market_briefings WHERE market=%s "
                "ORDER BY briefing_date DESC, slot_time DESC, id DESC LIMIT %s",
                (market, limit * 5),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, market, slot, briefing_date, slot_time, summary, context_json, model, created_at "
                "FROM market_briefings "
                "ORDER BY briefing_date DESC, slot_time DESC, id DESC LIMIT %s",
                (limit * 5,),
            ).fetchall()
    finally:
        conn.close()
    groups = []
    current_date = None
    current_bucket = None
    for r in rows:
        item = _brief_row_to_item(r)
        date = item.get("briefing_date") or ""
        if date != current_date:
            if current_bucket and len(groups) >= limit:
                break
            current_date = date
            current_bucket = {"date": date, "items": []}
            groups.append(current_bucket)
        current_bucket["items"].append(item)
    return {"groups": groups[:limit], "total_rows": len(rows)}


@app.get("/api/stocks/{code}/daily-summary")
def get_stock_daily_summary(code: str):
    from server.db.connections import get_stocks_conn
    import json as _json
    conn = get_stocks_conn()
    try:
        row = conn.execute(
            "SELECT code, summary_date, one_liner, drivers_json, tone, "
            "used_signals_json, model, status, updated_at "
            "FROM stock_daily_summary "
            "WHERE code=%s AND status='ok' "
            "ORDER BY summary_date DESC, updated_at DESC LIMIT 1",
            (code,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"code": code, "available": False}
    drivers = []
    used = []
    try:
        drivers = _json.loads(row[3] or "[]")
    except Exception:
        pass
    try:
        used = _json.loads(row[5] or "[]")
    except Exception:
        pass
    return {
        "code": row[0],
        "available": True,
        "summary_date": _jsonable(row[1]),
        "one_liner": row[2] or "",
        "drivers": drivers,
        "tone": row[4] or "",
        "used_signals": used,
        "model": row[6] or "",
        "updated_at": _jsonable(row[8]),
    }


# [2026-05-18 정리] /api/themes/{theme_name}/context 핸들러 제거 —
# part02_list_theme_market.py 의 @router.get("/api/themes/{theme_name:path}/context")
# 가 먼저 등록되어 우선 매칭되므로 이 핸들러는 도달 불가능한 죽은 코드였음.


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
