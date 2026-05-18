"""KIS 실시간 구독 추적 원장 (Subscription Ledger).

누가 어떤 종목 코드를 보고 있는지 (HTTP/WS 양쪽) 추적하기 위한 self-contained
원장. Sweeper 가 주기적으로 stale code 를 orderbook hub 에서 제거하는데,
sweeper 자체는 hub 에 의존하므로 part01 에 남아 있고 이 모듈은 데이터 구조만
제공한다.

원래 server/routes/stocks_parts/part01_realtime_base.py 에 있던 코드를 분리.
"""

import os
import threading
import time


class KisSubscriptionLedger:
    IDLE_SEC = max(60.0, float(os.getenv("KIS_SUB_IDLE_SEC", "600")))
    DWELL_SEC = max(30.0, float(os.getenv("KIS_SUB_DWELL_SEC", "120")))
    MAX_CODES = max(10, int(os.getenv("KIS_MAX_SUBSCRIPTIONS", "100")))

    def __init__(self):
        self._lock = threading.Lock()
        # code → {first_ts, last_ts, sources:set[str]}
        self._records: dict[str, dict] = {}

    def touch(self, code: str, source: str = "unknown") -> None:
        code = str(code or "").strip()
        if not code:
            return
        now_ts = time.time()
        with self._lock:
            rec = self._records.get(code)
            if rec is None:
                rec = {"first_ts": now_ts, "last_ts": now_ts, "sources": set()}
                self._records[code] = rec
            rec["last_ts"] = now_ts
            if source:
                try:
                    rec["sources"].add(str(source))
                except Exception:
                    pass

    def snapshot(self, now_ts: float | None = None) -> tuple[set[str], set[str]]:
        """Return (active, stale)."""
        now_ts = float(time.time() if now_ts is None else now_ts)
        active: set[str] = set()
        stale: set[str] = set()
        with self._lock:
            for code, rec in list(self._records.items()):
                age = now_ts - float(rec.get("last_ts") or 0)
                dwell = now_ts - float(rec.get("first_ts") or 0)
                if age > self.IDLE_SEC and dwell > self.DWELL_SEC:
                    stale.add(code)
                else:
                    active.add(code)
        return active, stale

    def active_codes(self, cap: int | None = None) -> list[str]:
        cap = int(self.MAX_CODES if cap is None else cap)
        now_ts = time.time()
        with self._lock:
            items = [
                (code, float(rec.get("last_ts") or 0))
                for code, rec in self._records.items()
                if (now_ts - float(rec.get("last_ts") or 0)) <= self.IDLE_SEC
            ]
        items.sort(key=lambda x: x[1], reverse=True)  # LRU: 최근 touch 순
        return [c for c, _ in items[:cap]]

    def prune_stale(self) -> int:
        """Remove stale records. Returns count removed."""
        _, stale = self.snapshot()
        if not stale:
            return 0
        with self._lock:
            for code in stale:
                self._records.pop(code, None)
        return len(stale)

    def size(self) -> int:
        with self._lock:
            return len(self._records)


# Backward-compat alias for existing call sites that referenced the original
# private name `_KisSubscriptionLedger`.
_KisSubscriptionLedger = KisSubscriptionLedger
