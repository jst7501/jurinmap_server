def _home_snapshot_ttls(market_status: str) -> tuple[int, int]:
    if market_status == "open":
        return 10, 15
    return 30, 90


def _home_snapshot_redis_key(market_status: str) -> str:
    return f"home:snapshot:{market_status}:v1"


_HOME_SNAPSHOT_REDIS_LATEST_KEY = "home:snapshot:latest:v1"
_HOME_SNAPSHOT_REDIS_STALE_KEY = "home:snapshot:stale:v1"
_HOME_SNAPSHOT_STALE_TTL_SEC = max(300, int(os.getenv("HOME_SNAPSHOT_STALE_TTL_SEC", "21600")))
_HOME_SNAPSHOT_MAX_STALE_SEC = max(15, int(os.getenv("HOME_SNAPSHOT_MAX_STALE_SEC", "180")))
_HOME_SNAPSHOT_WAIT_ON_MISS_SEC = max(0.0, float(os.getenv("HOME_SNAPSHOT_WAIT_ON_MISS_SEC", "0.25")))


def _home_snapshot_defaults() -> dict:
    return {
        "indices": {},
        "market_brief": {"text": None},
        "market_signal": {},
        "themes": {"rising": [], "hot": []},
        "ranking_strength": {"items": [], "count": 0},
        "macro": {},
        "ranking_surge": {"items": []},
        "ranking_program": {"items": [], "items_sell": []},
        "ranking_short": {"items": []},
        "vi_status": {"items": []},
        "timeline": {"events": []},
        "global_indicators": {"categories": {}, "updated_at": "", "total": 0},
        "ranking_volume": {"kospi": [], "kosdaq": [], "updated_at": None},
    }


def _home_snapshot_warmup_payload(market_status: str, reason: str) -> dict:
    payload = {
        **_home_snapshot_defaults(),
        "updated_at": datetime.now().strftime("%H:%M:%S"),
        "market_status": market_status,
        "source": "server_aggregate_v1",
        "_stale": True,
        "error": reason,
    }
    return _home_json_safe(payload)


def _log_home_snapshot_serve(source: str, started: float) -> None:
    elapsed_ms = int((time.time() - started) * 1000)
    if elapsed_ms >= 120:
        logger.info("[home-snapshot] served source=%s elapsed_ms=%d", source, elapsed_ms)


def _home_snapshot_cache_put(payload: dict, ts: float | None = None) -> None:
    with _HOME_SNAPSHOT_LOCK:
        _HOME_SNAPSHOT_CACHE["data"] = payload
        _HOME_SNAPSHOT_CACHE["ts"] = float(ts if ts is not None else time.time())


def _home_snapshot_redis_get_best(redis_key: str) -> dict | None:
    for key in (redis_key, _HOME_SNAPSHOT_REDIS_LATEST_KEY, _HOME_SNAPSHOT_REDIS_STALE_KEY):
        hit = redis_get_json(key)
        if isinstance(hit, dict):
            return hit
    return None


def _home_snapshot_try_claim() -> threading.Event | None:
    global _HOME_SNAPSHOT_INFLIGHT
    with _HOME_SNAPSHOT_LOCK:
        if _HOME_SNAPSHOT_INFLIGHT is not None:
            return None
        ev = threading.Event()
        _HOME_SNAPSHOT_INFLIGHT = ev
        return ev


def _home_snapshot_release(ev: threading.Event | None) -> None:
    global _HOME_SNAPSHOT_INFLIGHT
    with _HOME_SNAPSHOT_LOCK:
        if _HOME_SNAPSHOT_INFLIGHT is ev:
            _HOME_SNAPSHOT_INFLIGHT = None
    if ev is not None:
        ev.set()


def _build_home_snapshot_payload(market_status: str) -> dict:
    errors = {}
    errors_lock = threading.Lock()

    def _fetch_global_indicators():
        # 외부 라우트 함수를 지연 import — 순환 import 방지
        from server.routes.global_indicators import list_global_indicators
        return list_global_indicators()

    tasks = [
        ("indices",          lambda: get_indices(),                              {}),
        ("market_brief",     lambda: get_market_brief(),                         {"text": None}),
        ("market_signal",    lambda: get_market_signal(),                        {}),
        ("themes",           lambda: get_themes(15),                             {"rising": [], "hot": []}),
        ("ranking_strength", lambda: get_ranking_strength(top=10, scan=25),      {"items": [], "count": 0}),
        ("macro",            lambda: get_macro(),                                 {}),
        ("ranking_surge",    lambda: get_ranking_surge(),                         {"items": []}),
        ("ranking_program",  lambda: get_ranking_program(limit=10),              {"items": [], "items_sell": []}),
        ("ranking_short",    lambda: get_ranking_short(limit=10, min_ratio=0.1), {"items": []}),
        ("vi_status",        lambda: get_vi_status(),                             {"items": []}),
        ("timeline",         lambda: get_timeline(),                              {"events": []}),
        ("global_indicators", _fetch_global_indicators,                           {"categories": {}, "updated_at": "", "total": 0}),
        # ranking_volume: 프론트 HTTP polling 제거용. get_ranking_volume() 자체가 mem+Redis 캐시 첫번째로 조회하므로 추가 부하 없음.
        ("ranking_volume",   lambda: get_ranking_volume(),                        {"kospi": [], "kosdaq": [], "updated_at": None}),
    ]

    def _run_task(task):
        name, fn, fallback = task
        try:
            return name, fn()
        except Exception as e:
            with errors_lock:
                errors[name] = str(e)
            return name, fallback

    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        results = dict(pool.map(_run_task, tasks))

    payload = {
        **results,
        "updated_at": datetime.now().strftime("%H:%M:%S"),
        "market_status": market_status,
        "source": "server_aggregate_v1",
    }
    if errors:
        payload["partial_errors"] = errors
    return _home_json_safe(payload)


def _persist_home_snapshot(payload: dict, market_status: str) -> None:
    now_ts = time.time()
    _, redis_ttl = _home_snapshot_ttls(market_status)
    redis_key = _home_snapshot_redis_key(market_status)
    _home_snapshot_cache_put(payload, ts=now_ts)
    redis_set_json(redis_key, payload, ttl_seconds=redis_ttl)
    redis_set_json(_HOME_SNAPSHOT_REDIS_LATEST_KEY, payload, ttl_seconds=max(redis_ttl, 120))
    redis_set_json(_HOME_SNAPSHOT_REDIS_STALE_KEY, payload, ttl_seconds=_HOME_SNAPSHOT_STALE_TTL_SEC)


def _refresh_home_snapshot_sync(reason: str = "manual") -> dict:
    ev = _home_snapshot_try_claim()
    if ev is None:
        with _HOME_SNAPSHOT_LOCK:
            cached = _HOME_SNAPSHOT_CACHE.get("data")
        if isinstance(cached, dict):
            return cached
        raise RuntimeError("home snapshot refresh already running")

    started = time.time()
    market_status = _market_status()
    try:
        payload = _build_home_snapshot_payload(market_status)
        _touch_focus_from_payload(payload, ttl_sec=120.0, max_collect=320)
        _persist_home_snapshot(payload, market_status)
        home_ws_push_from_thread(payload)
        elapsed_ms = int((time.time() - started) * 1000)
        if elapsed_ms >= 200:
            logger.info("[home-snapshot] refresh ok reason=%s elapsed_ms=%d", reason, elapsed_ms)
        return payload
    finally:
        _home_snapshot_release(ev)


def _refresh_home_snapshot_async(reason: str = "api") -> bool:
    ev = _home_snapshot_try_claim()
    if ev is None:
        return False

    def _worker(local_ev: threading.Event) -> None:
        started = time.time()
        market_status = _market_status()
        try:
            payload = _build_home_snapshot_payload(market_status)
            _touch_focus_from_payload(payload, ttl_sec=120.0, max_collect=320)
            _persist_home_snapshot(payload, market_status)
            home_ws_push_from_thread(payload)
            elapsed_ms = int((time.time() - started) * 1000)
            if elapsed_ms >= 200:
                logger.info("[home-snapshot] async refresh ok reason=%s elapsed_ms=%d", reason, elapsed_ms)
        except Exception as e:
            logger.warning("[home-snapshot] async refresh failed reason=%s err=%s", reason, e)
        finally:
            _home_snapshot_release(local_ev)

    threading.Thread(target=_worker, args=(ev,), daemon=True, name="home-snapshot-refresh").start()
    return True


@router.get("/api/home/snapshot")
def get_home_snapshot():
    market_status = _market_status()
    mem_ttl, _ = _home_snapshot_ttls(market_status)
    redis_key = _home_snapshot_redis_key(market_status)
    started = time.time()
    now_ts = started

    with _HOME_SNAPSHOT_LOCK:
        mem_data = _HOME_SNAPSHOT_CACHE.get("data")
        mem_ts = float(_HOME_SNAPSHOT_CACHE.get("ts") or 0.0)

    if isinstance(mem_data, dict):
        age = now_ts - mem_ts
        if age < mem_ttl:
            _touch_focus_from_payload(mem_data, ttl_sec=120.0, max_collect=320)
            _log_home_snapshot_serve("mem_fresh", started)
            return mem_data
        if age < _HOME_SNAPSHOT_MAX_STALE_SEC:
            _refresh_home_snapshot_async(reason="stale_mem")
            stale = copy.deepcopy(mem_data)
            stale["_stale"] = True
            stale["snapshot_age_sec"] = int(age)
            _touch_focus_from_payload(stale, ttl_sec=120.0, max_collect=320)
            _log_home_snapshot_serve("mem_stale", started)
            return stale

    redis_hit = _home_snapshot_redis_get_best(redis_key)
    if isinstance(redis_hit, dict):
        _home_snapshot_cache_put(redis_hit, ts=now_ts)
        _touch_focus_from_payload(redis_hit, ttl_sec=120.0, max_collect=320)
        _log_home_snapshot_serve("redis", started)
        return redis_hit

    _refresh_home_snapshot_async(reason="cache_miss")

    with _HOME_SNAPSHOT_LOCK:
        inflight = _HOME_SNAPSHOT_INFLIGHT
    if inflight is not None and _HOME_SNAPSHOT_WAIT_ON_MISS_SEC > 0:
        inflight.wait(timeout=_HOME_SNAPSHOT_WAIT_ON_MISS_SEC)
        with _HOME_SNAPSHOT_LOCK:
            after_data = _HOME_SNAPSHOT_CACHE.get("data")
        if isinstance(after_data, dict):
            _touch_focus_from_payload(after_data, ttl_sec=120.0, max_collect=320)
            _log_home_snapshot_serve("wait_inflight", started)
            return after_data

    if isinstance(mem_data, dict):
        stale = copy.deepcopy(mem_data)
        stale["_stale"] = True
        stale["snapshot_age_sec"] = int(max(0.0, now_ts - mem_ts))
        stale["error"] = "cache_refreshing"
        _log_home_snapshot_serve("mem_fallback", started)
        return stale

    payload = _home_snapshot_warmup_payload(market_status, reason="warming_up")
    _log_home_snapshot_serve("warmup", started)
    return payload


# ─── /ws/home ─────────────────────────────────────────────────────────────────
# - seq: 단조 증가 번호로 클라이언트 역전 방지
# - throttle: 최소 5초 간격으로 broadcast (prewarmer 과호출 방어)
# - partial: 섹션별 타입으로 분리 전송하여 over-rendering 최소화

_HOME_WS_CLIENTS: dict[int, "WebSocket"] = {}
_HOME_WS_NEXT_ID = 0
_HOME_WS_LOOP: asyncio.AbstractEventLoop | None = None
_HOME_WS_LAST_ACTIVE: dict[int, float] = {}
_HOME_WS_FINGERPRINT: dict[int, str] = {}
_HOME_WS_IDLE_SEC = max(30.0, float(os.getenv("HOME_WS_IDLE_SEC", "90.0")))

# throttle / seq 상태
_HOME_WS_SEQ = 0
_HOME_WS_LAST_BROADCAST_TS: float = 0.0
_HOME_WS_BROADCAST_MIN_INTERVAL = float(os.getenv("HOME_WS_BROADCAST_INTERVAL_SEC", "5"))

# 섹션 → WS type 매핑 (partial update용)
_HOME_WS_SECTION_TYPES: dict[str, str] = {
    "indices":          "home:indices",
    "market_signal":    "home:signal",
    "market_brief":     "home:brief",
    "themes":           "home:themes",
    "ranking_strength": "home:rank_strength",
    "ranking_surge":    "home:rank_surge",
    "ranking_program":  "home:rank_program",
    "ranking_short":    "home:rank_short",
    "macro":            "home:macro",
    "vi_status":        "home:vi",
    "timeline":         "home:timeline",
    "global_indicators": "home:global_indicators",
    "ranking_volume":    "home:rank_volume",
}

# section fingerprint 캐시 — 변한 섹션만 broadcast (R3 대응)
# 이전 broadcast 의 각 섹션 hash. 같은 hash 면 해당 섹션 message 생략.
_HOME_WS_SECTION_HASH: dict[str, str] = {}

def _section_hash(section: str, data) -> str:
    """섹션 데이터의 content-addressable hash. 캐시 키로만 사용."""
    import hashlib
    try:
        raw = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    except Exception:
        raw = str(data).encode("utf-8", errors="replace")
    return hashlib.sha1(raw).hexdigest()


def _home_json_safe(value):
    """
    WebSocket/Redis 직렬화 전에 Decimal 등 비-JSON 타입을 안전하게 변환한다.
    """
    try:
        return jsonable_encoder(
            value,
            custom_encoder={
                Decimal: lambda d: float(d),
            },
        )
    except Exception:
        return value


def _home_ws_make_fingerprint(ws: WebSocket) -> str:
    try:
        xff = str((ws.headers.get("x-forwarded-for") or "")).split(",")[0].strip()
    except Exception:
        xff = ""
    try:
        host = str((ws.client.host if ws.client else "") or "").strip()
    except Exception:
        host = ""
    try:
        ua = str((ws.headers.get("user-agent") or "") or "").strip()
    except Exception:
        ua = ""
    ip = xff or host or "unknown"
    return f"{ip}|{ua[:96]}"


def _home_ws_touch(cid: int) -> None:
    _HOME_WS_LAST_ACTIVE[cid] = time.monotonic()


def _home_ws_active_cids() -> list[int]:
    now = time.monotonic()
    out = []
    for cid in list(_HOME_WS_CLIENTS.keys()):
        last = float(_HOME_WS_LAST_ACTIVE.get(cid) or 0.0)
        if now - last <= _HOME_WS_IDLE_SEC:
            out.append(cid)
    return out


def _home_ws_count_active_users() -> int:
    users = set()
    for cid in _home_ws_active_cids():
        fp = str(_HOME_WS_FINGERPRINT.get(cid) or "").strip()
        users.add(fp or f"cid:{cid}")
    return len(users)


async def _home_ws_prune_stale() -> None:
    now = time.monotonic()
    stale = []
    for cid, ws in list(_HOME_WS_CLIENTS.items()):
        last = float(_HOME_WS_LAST_ACTIVE.get(cid) or 0.0)
        if now - last > _HOME_WS_IDLE_SEC:
            stale.append((cid, ws))
    for cid, ws in stale:
        _HOME_WS_CLIENTS.pop(cid, None)
        _HOME_WS_LAST_ACTIVE.pop(cid, None)
        _HOME_WS_FINGERPRINT.pop(cid, None)
        try:
            await asyncio.wait_for(ws.close(code=1001, reason="home_idle_timeout"), timeout=0.5)
        except Exception:
            pass


async def _home_ws_send_all(messages: list[dict]):
    """모든 클라이언트에 메시지 목록 순서대로 전송. 끊긴 클라이언트 정리."""
    if not _HOME_WS_CLIENTS or not messages:
        return
    await _home_ws_prune_stale()
    if not _HOME_WS_CLIENTS:
        return
    dead = []
    for cid, ws in list(_HOME_WS_CLIENTS.items()):
        try:
            for msg in messages:
                await ws.send_json(msg)
        except Exception as e:
            logger.debug("[ws/home] broadcast send failed cid=%s err=%s", cid, e)
            dead.append(cid)
    for cid in dead:
        _HOME_WS_CLIENTS.pop(cid, None)
        _HOME_WS_LAST_ACTIVE.pop(cid, None)
        _HOME_WS_FINGERPRINT.pop(cid, None)


def _build_partial_messages(payload: dict, seq: int, *, force_full: bool = False) -> list[dict]:
    """Build section-based partial WS messages from snapshot payload.

    R3 대응: 각 섹션의 hash 를 이전 broadcast 와 비교해 **변한 섹션만** 포함.
    force_full=True 면 모든 섹션 포함 (신규 클라이언트 onboarding 등).
    meta/stats 는 매번 포함 (경량이고 항상 갱신 필요).
    """
    msgs = []
    global _HOME_WS_SECTION_HASH
    changed_sections = []
    skipped_sections = []
    for section, ws_type in _HOME_WS_SECTION_TYPES.items():
        data = payload.get(section)
        if data is None:
            continue
        safe_data = _home_json_safe(data)
        h = _section_hash(section, safe_data)
        prev_h = _HOME_WS_SECTION_HASH.get(section)
        if not force_full and prev_h == h:
            skipped_sections.append(section)
            continue
        _HOME_WS_SECTION_HASH[section] = h
        changed_sections.append(section)
        msgs.append({"type": ws_type, "seq": seq, "data": safe_data})
    if skipped_sections and logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "[ws/home] section dedup: changed=%s skipped=%s",
            changed_sections, skipped_sections,
        )
    msgs.append({
        "type": "home:meta",
        "seq": seq,
        "updated_at": _home_json_safe(payload.get("updated_at")),
        "market_status": _home_json_safe(payload.get("market_status")),
    })
    # 접속자 통계 — 프론트에서 /ws/news-prices 별도 연결 불필요하게 함
    msgs.append({
        "type": "home:stats",
        "seq": seq,
        "data": {
            "active_connections": _display_live_users_count(_home_ws_count_active_users()),
            "max_connections": _NEWS_WS_MAX_CLIENTS,
            "beta_notice": _NEWS_WS_BETA_NOTICE,
        },
    })
    return msgs

async def _home_ws_broadcast(payload: dict):
    global _HOME_WS_SEQ, _HOME_WS_LAST_BROADCAST_TS
    _HOME_WS_SEQ += 1
    _HOME_WS_LAST_BROADCAST_TS = time.time()
    msgs = _build_partial_messages(payload, _HOME_WS_SEQ)
    await _home_ws_send_all(msgs)


def home_ws_push_from_thread(payload: dict):
    """백그라운드 스레드(prewarm 등)에서 호출 — throttle 후 event loop에 예약."""
    global _HOME_WS_LAST_BROADCAST_TS
    loop = _HOME_WS_LOOP
    if not loop or not loop.is_running() or not _HOME_WS_CLIENTS:
        return
    if time.time() - _HOME_WS_LAST_BROADCAST_TS < _HOME_WS_BROADCAST_MIN_INTERVAL:
        return  # throttle
    asyncio.run_coroutine_threadsafe(_home_ws_broadcast(payload), loop)


@router.websocket("/ws/home")
async def ws_home(websocket: WebSocket):
    global _HOME_WS_NEXT_ID, _HOME_WS_LOOP, _HOME_WS_SEQ
    _HOME_WS_LOOP = asyncio.get_event_loop()

    if await reject_websocket_if_unauthorized(websocket):
        return

    await websocket.accept()
    cid = _HOME_WS_NEXT_ID
    _HOME_WS_NEXT_ID += 1
    _HOME_WS_CLIENTS[cid] = websocket
    _HOME_WS_FINGERPRINT[cid] = _home_ws_make_fingerprint(websocket)
    _home_ws_touch(cid)

    with _HOME_SNAPSHOT_LOCK:
        cached = _HOME_SNAPSHOT_CACHE.get("data")

    init_seq = _HOME_WS_SEQ
    if isinstance(cached, dict):
        try:
            for msg in _build_partial_messages(cached, init_seq):
                await websocket.send_json(msg)
        except Exception as e:
            logger.warning("[ws/home] init send failed cid=%s err=%s", cid, e)
            _HOME_WS_CLIENTS.pop(cid, None)
            _HOME_WS_LAST_ACTIVE.pop(cid, None)
            _HOME_WS_FINGERPRINT.pop(cid, None)
            return
    else:
        try:
            await websocket.send_json({
                "type": "home:meta",
                "seq": init_seq,
                "updated_at": None,
                "market_status": _market_status(),
            })
            await websocket.send_json({
                "type": "home:stats",
                "seq": init_seq,
                "data": {
                    "active_connections": _display_live_users_count(_home_ws_count_active_users()),
                    "max_connections": _NEWS_WS_MAX_CLIENTS,
                    "beta_notice": _NEWS_WS_BETA_NOTICE,
                },
            })
        except Exception as e:
            logger.warning("[ws/home] warmup send failed cid=%s err=%s", cid, e)
            _HOME_WS_CLIENTS.pop(cid, None)
            _HOME_WS_LAST_ACTIVE.pop(cid, None)
            _HOME_WS_FINGERPRINT.pop(cid, None)
            return

        async def _send_deferred_snapshot(target_cid: int, target_ws: WebSocket) -> None:
            try:
                # Reuse cache-first snapshot path to avoid per-client heavy recompute.
                payload = await asyncio.to_thread(get_home_snapshot)
                if target_cid not in _HOME_WS_CLIENTS:
                    return
                seq = _HOME_WS_SEQ
                for msg in _build_partial_messages(payload, seq):
                    await target_ws.send_json(msg)
            except Exception:
                return

        asyncio.create_task(_send_deferred_snapshot(cid, websocket))

    try:
        while True:
            raw = await websocket.receive_text()
            _home_ws_touch(cid)
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("type") == "ping":
                await websocket.send_json({
                    "type": "pong",
                    "seq": _HOME_WS_SEQ,
                    "stats": {
                        "active_connections": _display_live_users_count(_home_ws_count_active_users()),
                        "max_connections": _NEWS_WS_MAX_CLIENTS,
                        "beta_notice": _NEWS_WS_BETA_NOTICE,
                    },
                })
    except WebSocketDisconnect:
        pass
    finally:
        _HOME_WS_CLIENTS.pop(cid, None)
        _HOME_WS_LAST_ACTIVE.pop(cid, None)
        _HOME_WS_FINGERPRINT.pop(cid, None)


def run_redis_prewarm() -> dict:
    started = time.time()
    steps = []

    def _run_step(name: str, fn):
        t0 = time.time()
        try:
            payload = fn()
            steps.append({
                "name": name,
                "ok": True,
                "elapsed_ms": int((time.time() - t0) * 1000),
                "size": len(payload) if isinstance(payload, dict) else None,
            })
        except Exception as e:
            steps.append({
                "name": name,
                "ok": False,
                "elapsed_ms": int((time.time() - t0) * 1000),
                "error": str(e),
            })

    _run_step("ranking_volume", lambda: get_ranking_volume())
    _run_step("ranking_fluctuation", lambda: get_ranking_fluctuation())
    _run_step("ranking_strength", lambda: get_ranking_strength(top=10, scan=25))
    _run_step("ranking_investor_foreign", lambda: get_ranking_investor(type="foreign", limit=10))
    _run_step("ranking_investor_institution", lambda: get_ranking_investor(type="institution", limit=10))
    _run_step("ranking_program", lambda: get_ranking_program(limit=10))
    _run_step("ranking_short", lambda: get_ranking_short(limit=10, min_ratio=0.1))
    _run_step("home_snapshot", lambda: _refresh_home_snapshot_sync(reason="prewarm"))
    _run_step("timeline", lambda: get_timeline())
    _run_step("vi_status", lambda: get_vi_status())
    _run_step("limit_up_break", lambda: get_limit_up_break_alerts(limit=20, force_refresh=True))

    ok_count = sum(1 for s in steps if s.get("ok"))
    return {
        "ok": ok_count == len(steps),
        "step_count": len(steps),
        "ok_count": ok_count,
        "steps": steps,
        "elapsed_ms": int((time.time() - started) * 1000),
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.post("/api/admin/prewarm")
def prewarm_redis_cache():
    return run_redis_prewarm()


# ─── Phase 5: KIS 운영 상태 엔드포인트 ─────────────────────────
@router.get("/api/ops/kis-status")
def get_kis_ops_status(window_sec: int = 300):
    """KIS 인프라 운영 대시보드용 상태 스냅샷.

    반환:
      - degrade_flag: is_kis_degraded() 현재 값
      - kis_rest: 최근 window 의 REST 호출 통계
      - kis_ws: TR별 tick/reconnect/마지막 tick 경과 카운터
      - cache_reads: surface별 ws_hit/rest_hit/miss 분포
      - subscription_ledger: 활성 구독 규모
      - breakers: 현재 쿨다운 중인 per-code breaker 요약
      - hubs: 두 KIS WS Hub 의 code 수 / 활성 여부
    """
    # 지연 import — monitoring 내부 함수는 모듈 레벨에 정의됨.
    # 주의: part08 은 stocks.py 의 globals 로 exec 되므로 '..monitoring' = 'server.monitoring'.
    from ..monitoring import (
        is_kis_degraded as _is_kis_degraded,
        _window_kis_stats, _window_ws_stats, _window_cache_stats,
    )
    now_ts = time.time()
    w = max(30, int(window_sec or 300))

    # Hub 상태
    def _hub_status(hub, enabled: bool) -> dict:
        try:
            with hub._lock:
                codes = list(hub._codes)
            return {
                "enabled": bool(enabled),
                "tr_id": hub.TR_ID,
                "max_codes": hub.MAX_CODES,
                "subscription_count": len(codes),
                "codes_sample": codes[:10],
                "ws_connected": bool(getattr(hub, "_ws", None) is not None),
                "has_approval_key": bool(getattr(hub, "_approval_key", None)),
            }
        except Exception as e:
            return {"enabled": bool(enabled), "error": str(e)}

    # Breaker 상태
    try:
        with _KIS_REST_BREAKER_LOCK:
            tripped = [
                {
                    "key": k,
                    "fail_count": int(v.get("fail_count") or 0),
                    "next_retry_in_sec": round(max(0.0, float(v.get("next_retry_ts") or 0.0) - now_ts), 2),
                    "last_error": str(v.get("last_error") or "")[:120],
                }
                for k, v in _KIS_REST_BREAKER_STATE.items()
                if float(v.get("next_retry_ts") or 0.0) > now_ts
            ]
    except Exception:
        tripped = []

    # Ledger 스냅샷
    try:
        active_codes, stale_codes = _SUB_LEDGER.snapshot(now_ts)
        ledger_stats = {
            "size": _SUB_LEDGER.size(),
            "active_count": len(active_codes),
            "stale_count": len(stale_codes),
            "idle_sec_threshold": _KisSubscriptionLedger.IDLE_SEC,
            "max_codes": _KisSubscriptionLedger.MAX_CODES,
            "sweep_enabled": bool(_SUB_LEDGER_SWEEP_ENABLED),
        }
    except Exception as e:
        ledger_stats = {"error": str(e)}

    return {
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window_sec": w,
        "degrade_flag": bool(_is_kis_degraded()),
        "kis_rest": _window_kis_stats(now_ts, w),
        "kis_ws": _window_ws_stats(now_ts, w),
        "cache_reads": _window_cache_stats(now_ts, w),
        "subscription_ledger": ledger_stats,
        "breakers_tripped": tripped[:20],
        "breakers_tripped_count": len(tripped),
        "hubs": {
            "rt_h0stcnt0": _hub_status(_KIS_RT_HUB, True),
            "orderbook_h0stasp0": _hub_status(_KIS_ORDERBOOK_HUB, _KIS_ORDERBOOK_WS_ENABLED),
            "nxt_h0nxcnt0": _hub_status(_KIS_NXT_RT_HUB, True),
        },
        "phase_flags": {
            "inflight_dedup_enabled": _LIVE_PRICE_DEDUP_ENABLED,
            "degrade_gates_pollers": os.getenv("KIS_DEGRADE_GATES_POLLERS", "1"),
            "price_handler_ws_first": _PRICE_HANDLER_WS_FIRST,
            "price_poller_exclude_subscribed": _PRICE_POLLER_EXCLUDE_SUBSCRIBED,
            "orderbook_ws_enabled": _KIS_ORDERBOOK_WS_ENABLED,
            "orderbook_ws_read": _KIS_ORDERBOOK_WS_READ,
            "subs_demand_driven": _SUB_LEDGER_SWEEP_ENABLED,
        },
    }


# ─── 홈 스냅샷 백그라운드 리프레셔 ─────────────────────────────────────────────
# 캐시 만료 전에 미리 갱신 → 사용자 요청 시 항상 캐시 히트 (<10ms)
_HOME_SNAP_BG_STARTED = False
_HOME_SNAP_BG_LOCK    = threading.Lock()


def start_home_snapshot_background_refresher():
    global _HOME_SNAP_BG_STARTED
    with _HOME_SNAP_BG_LOCK:
        if _HOME_SNAP_BG_STARTED:
            return
        _HOME_SNAP_BG_STARTED = True

    # Phase 4 — 유저 연결이 없을 때는 snapshot refresh 도 건너뜀 (KIS 부하 감소).
    # 마지막 연결 해제 후 linger_sec 동안은 계속 refresh (페이지 이동/리프레시 시 staleness 최소화).
    _home_gate_enabled = str(os.getenv("HOME_SNAPSHOT_IDLE_GATE", "1")).strip().lower() not in ("0", "false", "no", "off")
    _home_gate_linger_sec = max(5.0, float(os.getenv("HOME_SNAPSHOT_LINGER_SEC", "15")))

    def _loop():
        # 첫 실행은 2초 후 (서버 완전 기동 대기)
        time.sleep(2)
        degrade_log_ts = 0.0
        idle_log_ts = 0.0
        last_client_seen_ts = time.time()
        while True:
            ms = _market_status()
            interval = 8 if ms == "open" else 25
            # Phase 1 degrade gate: KIS 불안정 시 snapshot refresh 건너뜀.
            if _kis_rest_should_skip():
                if time.time() - degrade_log_ts > 60:
                    logger.info("[home-bg] snapshot refresh skipped (kis degraded)")
                    degrade_log_ts = time.time()
                time.sleep(max(30.0, float(interval)))
                continue

            # Phase 4 idle gate: WS home 클라이언트 없으면 refresh 안 함.
            # _HOME_WS_CLIENTS 는 dict (키: cid). 빈 dict = 연결자 0명.
            if _home_gate_enabled:
                has_clients = bool(_HOME_WS_CLIENTS)
                now_ts = time.time()
                if has_clients:
                    last_client_seen_ts = now_ts
                idle_for = now_ts - last_client_seen_ts
                if not has_clients and idle_for > _home_gate_linger_sec:
                    if now_ts - idle_log_ts > 60:
                        logger.info(
                            "[home-bg] idle (no ws clients for %.0fs) — skipping refresh",
                            idle_for,
                        )
                        idle_log_ts = now_ts
                    time.sleep(max(30.0, float(interval)))
                    continue

            try:
                _refresh_home_snapshot_sync(reason="bg_loop")
            except Exception as e:
                logger.warning("[home-bg] snapshot refresh error: %s", e)
                interval = 30
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True, name="home-snap-refresher")
    t.start()
    logger.info("[home-bg] home snapshot background refresher started")

