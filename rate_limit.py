"""Token-bucket rate limiting for ``/v1/chat/completions``.

Two layers, both implemented as a tiny in-process token bucket so we
avoid pulling in ``slowapi`` and its transitive dependencies:

* **Per-IP global bucket** — coarse DDoS / abuse protection.
* **Per-account bucket** — already handled inside ``AccountPool`` via the
  ``rate_limit_rpm`` field, but exposed here too so ``/api/stats`` can
  report it.

The bucketing is done with ``time.monotonic()`` so system clock jumps
(wall-clock NTP corrections, DST changes, etc.) cannot break the limit.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from logger import get_logger

log = get_logger("rate_limit")


@dataclass
class _Bucket:
    capacity: float
    refill_per_sec: float
    tokens: float
    last_refill: float

    def try_consume(self, amount: float = 1.0) -> bool:
        now = time.monotonic()
        elapsed = now - self.last_refill
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
            self.last_refill = now
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False


class RateLimiter:
    """Async-safe token-bucket collection keyed by ``str``."""

    def __init__(self, capacity: int, per_minute: int):
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()
        self.capacity = float(capacity)
        self.per_minute = per_minute
        self.refill_per_sec = per_minute / 60.0

    async def hit(self, key: str) -> bool:
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(
                    capacity=self.capacity,
                    refill_per_sec=self.refill_per_sec,
                    tokens=self.capacity,
                    last_refill=time.monotonic(),
                )
                self._buckets[key] = bucket
            ok = bucket.try_consume(1.0)
        if not ok:
            log.info("rate_limited", extra={"key": key})
        return ok


# ── FastAPI dependency ───────────────────────────────────────────────

def make_ip_limiter(requests_per_minute: int, burst: int | None = None) -> RateLimiter:
    cap = burst if burst is not None else max(1, requests_per_minute)
    return RateLimiter(capacity=cap, per_minute=requests_per_minute)




def ip_key(request: Request) -> str:
    """Bucket key: forwarded client IP if present, else socket peer."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return request.client.host if request.client else "unknown"


def make_rate_limit_dependency(limiter: RateLimiter):
    """Build a FastAPI dependency that enforces ``limiter`` per client IP."""

    async def _enforce(request: Request) -> None:
        key = ip_key(request)
        if not await limiter.hit(key):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"error": {"type": "rate_limited", "message": "Too many requests"}},
                headers={"Retry-After": "1"},
            )

    return _enforce
