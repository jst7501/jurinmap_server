from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

import requests
from fastapi import WebSocket

from config.settings import KIS_APP_KEY, KIS_APP_SECRET, KIS_DOMAIN
from utils.market_utils import get_us_market_status

try:
    import websockets
except Exception:  # pragma: no cover - runtime import guard
    websockets = None


logger = logging.getLogger("server.services.websocket_service")

TRADE_TR_ID = "H0GSCNT0"
ORDERBOOK_TR_ID = "H0GSAST0"
CHANNEL_TO_TR_ID = {
    "trade": TRADE_TR_ID,
    "orderbook": ORDERBOOK_TR_ID,
}


# 2026-05-18: server/core/numeric 로 통합
from server.core.numeric import to_float as _to_float


def _now_ts() -> float:
    return time.time()


def _extract_hms(fields: List[str]) -> str:
    for value in fields:
        text = str(value).strip()
        if len(text) == 6 and text.isdigit():
            return text
    return ""


class KISWebSocketProxy:
    def __init__(self) -> None:
        self.uri = os.getenv("KIS_OVERSEAS_WS_URL", "ws://ops.koreainvestment.com:21000")
        self.whale_min_notional_usd = float(os.getenv("WHALE_MIN_NOTIONAL_USD", "100000"))
        self.orderbook_emit_interval_sec = float(os.getenv("ORDERBOOK_EMIT_INTERVAL_SEC", "3"))

        self._clients: Dict[WebSocket, Dict[str, Set[str]]] = {}
        self._desired_pairs: Set[Tuple[str, str]] = set()
        self._upstream_pairs: Set[Tuple[str, str]] = set()

        self._lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()

        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._ws = None

        self._approval_key: Optional[str] = None
        self._approval_key_ts = 0.0

        self._last_orderbook_emit: Dict[str, float] = {}
        self._recent_trades: Dict[str, Deque[Tuple[float, float, float]]] = defaultdict(lambda: deque(maxlen=600))

    async def add_client(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.setdefault(websocket, {})
        await self._ensure_running()

    async def remove_client(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.pop(websocket, None)
            self._recompute_desired_locked()
        await self._sync_upstream()

        if not self._clients and self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def handle_client_message(self, websocket: WebSocket, text: str) -> None:
        payload: Dict[str, Any]
        try:
            parsed = json.loads(text)
            payload = parsed if isinstance(parsed, dict) else {}
        except Exception:
            payload = {"action": "subscribe", "symbol": str(text).strip()}

        action = str(payload.get("action") or "subscribe").strip().lower()
        symbols = self._normalize_symbols(payload)
        channels = self._normalize_channels(payload)

        if action == "ping":
            await websocket.send_text(json.dumps({"type": "pong", "ts": int(_now_ts())}))
            return

        if not symbols and action != "clear":
            await websocket.send_text(json.dumps({"type": "error", "message": "symbol is required"}))
            return

        async with self._lock:
            subscriptions = self._clients.setdefault(websocket, {})
            if action == "subscribe":
                for symbol in symbols:
                    subscriptions.setdefault(symbol, set()).update(channels)
            elif action == "unsubscribe":
                for symbol in symbols:
                    current = subscriptions.get(symbol, set())
                    current.difference_update(channels)
                    if not current:
                        subscriptions.pop(symbol, None)
            elif action == "clear":
                subscriptions.clear()
            else:
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "error",
                            "message": "action must be subscribe/unsubscribe/clear/ping",
                        }
                    )
                )
                return

            self._recompute_desired_locked()
            snapshot = self._client_snapshot_locked(websocket)

        await self._sync_upstream()
        await websocket.send_text(json.dumps({"type": "subscription_ack", "subscriptions": snapshot}))

    def status(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "clients": len(self._clients),
            "desired_subscriptions": len(self._desired_pairs),
            "upstream_subscriptions": len(self._upstream_pairs),
            "ws_uri": self.uri,
            "market_status": get_us_market_status(),
            "whale_min_notional_usd": self.whale_min_notional_usd,
        }

    def _normalize_symbols(self, payload: Dict[str, Any]) -> List[str]:
        symbol = payload.get("symbol")
        symbols = payload.get("symbols")

        merged: List[str] = []
        if isinstance(symbol, str) and symbol.strip():
            merged.append(symbol.strip().upper())
        if isinstance(symbols, list):
            for item in symbols:
                text = str(item or "").strip().upper()
                if text:
                    merged.append(text)

        return list(dict.fromkeys(merged))

    def _normalize_channels(self, payload: Dict[str, Any]) -> Set[str]:
        raw = payload.get("channels")
        if isinstance(raw, list):
            values = {str(item).strip().lower() for item in raw}
        elif isinstance(raw, str):
            values = {raw.strip().lower()}
        else:
            values = {"trade", "orderbook"}

        normalized = {v for v in values if v in CHANNEL_TO_TR_ID}
        return normalized or {"trade", "orderbook"}

    def _client_snapshot_locked(self, websocket: WebSocket) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for symbol, channels in sorted(self._clients.get(websocket, {}).items()):
            result.append({"symbol": symbol, "channels": sorted(channels)})
        return result

    def _recompute_desired_locked(self) -> None:
        desired: Set[Tuple[str, str]] = set()
        for subscriptions in self._clients.values():
            for symbol, channels in subscriptions.items():
                for channel in channels:
                    tr_id = CHANNEL_TO_TR_ID.get(channel)
                    if tr_id:
                        desired.add((tr_id, symbol))
        self._desired_pairs = desired

    async def _ensure_running(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self.connect_and_listen(), name="kis_overseas_ws_proxy")

    async def _sync_upstream(self) -> None:
        async with self._lock:
            desired = set(self._desired_pairs)
        if self._ws is None:
            return

        to_add = desired - self._upstream_pairs
        to_remove = self._upstream_pairs - desired

        if not to_add and not to_remove:
            return

        for tr_id, symbol in sorted(to_remove):
            await self._send_subscribe(tr_id, symbol, subscribe=False)
            self._upstream_pairs.discard((tr_id, symbol))

        for tr_id, symbol in sorted(to_add):
            await self._send_subscribe(tr_id, symbol, subscribe=True)
            self._upstream_pairs.add((tr_id, symbol))

    async def _get_approval_key(self) -> Optional[str]:
        # KIS approval key usually rotates. Refresh periodically.
        now = _now_ts()
        if self._approval_key and (now - self._approval_key_ts) < 60 * 30:
            return self._approval_key

        url = f"{KIS_DOMAIN.rstrip('/')}/oauth2/Approval"
        payload = {
            "grant_type": "client_credentials",
            "appkey": KIS_APP_KEY,
            "secretkey": KIS_APP_SECRET,
        }

        def _request() -> Optional[str]:
            try:
                response = requests.post(url, json=payload, timeout=10)
                if response.status_code != 200:
                    logger.warning("approval key request failed: HTTP %s", response.status_code)
                    return None
                data = response.json()
                return data.get("approval_key")
            except Exception as exc:
                logger.warning("approval key request exception: %s", exc)
                return None

        approval_key = await asyncio.to_thread(_request)
        if not approval_key:
            return None

        self._approval_key = approval_key
        self._approval_key_ts = _now_ts()
        return approval_key

    async def _send_subscribe(self, tr_id: str, symbol: str, subscribe: bool) -> None:
        if self._ws is None:
            return

        if not self._approval_key:
            return

        message = {
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",
                "tr_type": "1" if subscribe else "2",
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id": tr_id,
                    "tr_key": symbol,
                }
            },
        }

        async with self._send_lock:
            try:
                await self._ws.send(json.dumps(message))
            except Exception as exc:
                logger.warning("upstream subscribe send failed (%s %s): %s", tr_id, symbol, exc)

    async def connect_and_listen(self) -> None:
        if websockets is None:
            logger.error("websockets package is not installed; overseas WS proxy disabled")
            self._running = False
            return

        while self._running:
            if not self._clients:
                await asyncio.sleep(1.0)
                continue

            approval_key = await self._get_approval_key()
            if not approval_key:
                await asyncio.sleep(5.0)
                continue

            try:
                async with websockets.connect(
                    self.uri,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=2000,
                ) as ws:
                    self._ws = ws
                    self._upstream_pairs.clear()
                    await self._sync_upstream()

                    logger.info("KIS overseas websocket connected")
                    while self._running:
                        message = await ws.recv()
                        if message is None:
                            break
                        await self._handle_upstream_message(message)
            except Exception as exc:
                logger.warning("KIS overseas websocket disconnected: %s", exc)
                await asyncio.sleep(3.0)
            finally:
                self._ws = None
                self._upstream_pairs.clear()

    async def _handle_upstream_message(self, message: Any) -> None:
        if not isinstance(message, str):
            return

        if message.startswith("{"):
            # ACK / heartbeat / error packet.
            await self._broadcast_system({"type": "KIS_WS_INFO", "raw": message})
            return

        if "|" not in message:
            return

        parts = message.split("|", 3)
        if len(parts) < 4:
            return

        tr_id = parts[1]
        payload = parts[3]
        fields = payload.split("^") if payload else []

        if tr_id == TRADE_TR_ID:
            event = self._parse_trade(fields)
            if not event:
                return
            await self._emit_trade_event(event)
            return

        if tr_id == ORDERBOOK_TR_ID:
            event = self._parse_orderbook(fields)
            if not event:
                return
            symbol = event.get("symbol", "")
            now = _now_ts()
            last_emit = self._last_orderbook_emit.get(symbol, 0.0)
            if now - last_emit < self.orderbook_emit_interval_sec:
                return
            self._last_orderbook_emit[symbol] = now
            await self._broadcast_to_interested(
                symbol=symbol,
                channel="orderbook",
                payload={"type": "ORDERBOOK", **event},
            )

    def _parse_trade(self, fields: List[str]) -> Optional[Dict[str, Any]]:
        if not fields:
            return None

        symbol = str(fields[0]).strip().upper()
        if not symbol:
            return None

        price = None
        qty = None

        for idx in (2, 3, 4, 10, 11):
            if idx < len(fields):
                value = _to_float(fields[idx])
                if value and value > 0:
                    price = value
                    break

        for idx in (12, 13, 14, 5, 6):
            if idx < len(fields):
                value = _to_float(fields[idx])
                if value and value > 0:
                    qty = value
                    break

        if price is None:
            for raw in fields[1:]:
                value = _to_float(raw)
                if value and value > 0:
                    price = value
                    break

        if qty is None:
            qty = 0.0

        if price is None:
            return None

        notional = price * qty
        return {
            "symbol": symbol,
            "trade_time": _extract_hms(fields),
            "last_price": price,
            "qty": qty,
            "notional_usd": notional,
            "market_status": get_us_market_status(),
            "raw_fields": fields,
        }

    def _parse_orderbook(self, fields: List[str]) -> Optional[Dict[str, Any]]:
        if not fields:
            return None

        symbol = str(fields[0]).strip().upper()
        if not symbol:
            return None

        values: List[float] = []
        for raw in fields[1:]:
            parsed = _to_float(raw)
            if parsed is not None:
                values.append(parsed)

        levels: List[Dict[str, Any]] = []
        if len(values) >= 40:
            ask_prices = values[0:10]
            bid_prices = values[10:20]
            ask_sizes = values[20:30]
            bid_sizes = values[30:40]
            for idx in range(10):
                levels.append(
                    {
                        "level": idx + 1,
                        "ask_price": ask_prices[idx],
                        "ask_size": ask_sizes[idx],
                        "bid_price": bid_prices[idx],
                        "bid_size": bid_sizes[idx],
                    }
                )

        return {
            "symbol": symbol,
            "market_status": get_us_market_status(),
            "timestamp": _extract_hms(fields),
            "levels": levels,
            "raw_fields": fields,
        }

    def _update_trade_stats(self, symbol: str, qty: float, notional: float) -> Dict[str, Any]:
        now = _now_ts()
        q = self._recent_trades[symbol]
        q.append((now, qty, notional))

        cutoff = now - 60.0
        while q and q[0][0] < cutoff:
            q.popleft()

        total_qty = sum(item[1] for item in q)
        odd_qty = sum(item[1] for item in q if item[1] < 100)
        odd_ratio = (odd_qty / total_qty) if total_qty > 0 else 0.0

        return {
            "odd_lot_ratio_1m": round(odd_ratio, 4),
            "is_odd_lot": qty < 100,
        }

    async def _emit_trade_event(self, event: Dict[str, Any]) -> None:
        symbol = str(event.get("symbol", "")).upper()
        qty = float(event.get("qty") or 0.0)
        notional = float(event.get("notional_usd") or 0.0)
        trade_stats = self._update_trade_stats(symbol, qty, notional)

        trade_payload = {
            "type": "TRADE",
            **event,
            **trade_stats,
            "is_whale": notional >= self.whale_min_notional_usd,
        }
        await self._broadcast_to_interested(symbol=symbol, channel="trade", payload=trade_payload)

        if notional >= self.whale_min_notional_usd:
            whale_payload = {
                "type": "WHALE_ALERT",
                "symbol": symbol,
                "amount": round(notional, 2),
                "price": event.get("last_price"),
                "qty": event.get("qty"),
                "trade_time": event.get("trade_time"),
                "market_status": event.get("market_status"),
                "threshold_usd": self.whale_min_notional_usd,
            }
            await self._broadcast_to_interested(symbol=symbol, channel="trade", payload=whale_payload)

    async def _broadcast_system(self, payload: Dict[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False)
        dead: List[WebSocket] = []
        for client in list(self._clients.keys()):
            try:
                await client.send_text(text)
            except Exception:
                dead.append(client)

        for client in dead:
            await self.remove_client(client)

    async def _broadcast_to_interested(self, symbol: str, channel: str, payload: Dict[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False)
        dead: List[WebSocket] = []

        for client, subscriptions in list(self._clients.items()):
            channels = set()
            channels.update(subscriptions.get(symbol, set()))
            channels.update(subscriptions.get("*", set()))
            if channel not in channels:
                continue
            try:
                await client.send_text(text)
            except Exception:
                dead.append(client)

        for client in dead:
            await self.remove_client(client)


kis_ws_proxy = KISWebSocketProxy()
