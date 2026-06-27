"""Tests for ``rate_limit.RateLimiter``."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from rate_limit import RateLimiter, make_rate_limit_dependency


def test_burst_capacity_then_429():
    rl = RateLimiter(capacity=2, per_minute=60)
    # First two hit within the burst window.
    assert asyncio.run(rl.hit("1.2.3.4")) is True
    assert asyncio.run(rl.hit("1.2.3.4")) is True
    # Third is rejected (bucket empty).
    assert asyncio.run(rl.hit("1.2.3.4")) is False


def test_separate_keys_have_separate_buckets():
    rl = RateLimiter(capacity=1, per_minute=60)
    assert asyncio.run(rl.hit("a")) is True
    assert asyncio.run(rl.hit("a")) is False
    assert asyncio.run(rl.hit("b")) is True  # independent bucket


def test_dependency_raises_on_excess():
    rl = RateLimiter(capacity=1, per_minute=60)
    dep = make_rate_limit_dependency(rl)
    class _Req:
        client = type("C", (), {"host": "9.9.9.9"})()
        headers: dict = {}
    req = _Req()
    # First call passes.
    asyncio.run(dep(request=req))
    # Second call to the same client raises.
    with pytest.raises(HTTPException) as exc:
        asyncio.run(dep(request=req))
    assert exc.value.status_code == 429


def test_dependency_honours_forwarded_for():
    rl = RateLimiter(capacity=1, per_minute=60)
    dep = make_rate_limit_dependency(rl)
    class _Req:
        client = type("C", (), {"host": "127.0.0.1"})()
        def __init__(self, hdrs): self.headers = hdrs
    a = _Req({"x-forwarded-for": "10.0.0.1, 10.0.0.2"})
    b = _Req({"x-forwarded-for": "10.0.0.2"})
    asyncio.run(dep(request=a))  # 10.0.0.1
    asyncio.run(dep(request=b))  # 10.0.0.2 — independent
    with pytest.raises(HTTPException):
        asyncio.run(dep(request=b))  # 10.0.0.2 over budget
