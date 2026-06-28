"""Tests for ``account_pool.AccountPool``."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from account_pool import Account, AccountPool, UsageTick


def _acct(name: str, **kwargs) -> Account:
    defaults = dict(
        name=name, f_sid="s", at="a", sn_param="n", bl_param="b", hl="zh-CN",
        bound_models=["gem"],
    )
    defaults.update(kwargs)
    return Account(**defaults)


def test_select_returns_account_for_bound_model():
    pool = AccountPool(strategy="least-recently-used")
    pool.add(_acct("a"))
    pool.add(_acct("b"))
    chosen = asyncio.run(pool.select("gem"))
    assert chosen is not None
    assert chosen.name in ("a", "b")


def test_disabled_account_is_skipped():
    pool = AccountPool()
    pool.add(_acct("a", enabled=False))
    chosen = asyncio.run(pool.select("gem"))
    assert chosen is None


def test_account_exceeding_error_budget_is_skipped():
    pool = AccountPool(max_errors=2)
    acct = _acct("a")
    acct.error_count = 5
    pool.add(acct)
    assert asyncio.run(pool.select("gem")) is None


def test_account_not_bound_to_model_is_skipped():
    pool = AccountPool()
    pool.add(_acct("a", bound_models=["other-model"]))
    assert asyncio.run(pool.select("gem")) is None


def test_lru_picks_oldest_first():
    pool = AccountPool(strategy="least-recently-used")
    a, b = _acct("a"), _acct("b")
    a.last_used = 100.0
    b.last_used = 50.0
    pool.add(a)
    pool.add(b)
    chosen = asyncio.run(pool.select("gem"))
    assert chosen is b


def test_first_strategy_returns_first_candidate():
    pool = AccountPool(strategy="first")
    pool.add(_acct("a", last_used=10))
    pool.add(_acct("b", last_used=0))
    chosen = asyncio.run(pool.select("gem"))
    assert chosen.name == "a"


def test_random_strategy_eventually_covers_both():
    pool = AccountPool(strategy="random")
    a, b = _acct("a"), _acct("b")
    pool.add(a)
    pool.add(b)
    seen = set()
    for _ in range(50):
        chosen = asyncio.run(pool.select("gem"))
        assert chosen is not None
        # Release before re-selecting; otherwise in-flight caps kick in.
        asyncio.run(pool.release(chosen))
        seen.add(chosen.name)
    assert seen == {"a", "b"}


def test_inflight_cap_blocks_when_reached():
    pool = AccountPool()
    a = _acct("a", max_concurrent=2)
    pool.add(a)
    first = asyncio.run(pool.select("gem"))
    second = asyncio.run(pool.select("gem"))
    third = asyncio.run(pool.select("gem"))
    assert first is a and second is a and third is None


def test_release_decrements_inflight():
    pool = AccountPool()
    a = _acct("a", max_concurrent=1)
    pool.add(a)
    first = asyncio.run(pool.select("gem"))
    assert asyncio.run(pool.select("gem")) is None  # capped
    asyncio.run(pool.release(first))
    assert asyncio.run(pool.select("gem")) is a


def test_rate_limit_blocks_after_burst():
    pool = AccountPool()
    a = _acct("a", rate_limit_rpm=2)
    pool.add(a)
    picks = [asyncio.run(pool.select("gem")) for _ in range(3)]
    assert picks[0] is not None
    assert picks[1] is not None
    assert picks[2] is None  # 3rd request in 60s exceeds budget


def test_record_success_resets_error_count():
    pool = AccountPool()
    a = _acct("a")
    a.error_count = 3
    pool.add(a)
    asyncio.run(pool.record_success(a))
    assert a.error_count == 0


def test_record_failure_increments_error_count():
    pool = AccountPool()
    a = _acct("a")
    pool.add(a)
    asyncio.run(pool.record_failure(a, reason="upstream 502"))
    assert a.error_count == 1
    assert a.last_error == "upstream 502"


def test_save_to_env_preserves_unrelated_keys(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "HOST=0.0.0.0\n"
        "PORT=1800\n"
        "API_KEY=secret\n"
        "ACCOUNT_legacy_F_SID=keepme\n",
        encoding="utf-8",
    )
    pool = AccountPool(env_path=env_file)
    pool.add(_acct("new", bound_models=["m1"]))
    pool.save_to_env(env_file)
    text = env_file.read_text(encoding="utf-8")
    assert "HOST=0.0.0.0" in text
    assert "API_KEY=secret" in text
    # The legacy account line is no longer present (we replaced it).
    assert "ACCOUNT_legacy_F_SID=keepme" not in text
    # The new account appears.
    assert "ACCOUNT_new_F_SID=s" in text
    assert "ACCOUNT_new_MODELS=m1" in text


# ── v1.1 additions ───────────────────────────────────────────────────


def test_lru_cold_start_eventually_covers_all_accounts():
    """With 5 fresh accounts (all last_used=0) the LRU strategy must
    rotate through them rather than always picking the first one."""
    pool = AccountPool(strategy="least-recently-used")
    for i in range(5):
        pool.add(_acct(f"a{i}"))
    seen = set()
    for _ in range(30):
        a = asyncio.run(pool.select("gem"))
        assert a is not None
        asyncio.run(pool.release(a))
        seen.add(a.name)
    assert len(seen) == 5  # all 5 accounts picked at least once


def test_stats_under_concurrent_select_does_not_raise():
    """``stats()`` must be safe to call while other coroutines are mutating."""
    pool = AccountPool()
    for i in range(10):
        pool.add(_acct(f"a{i}"))
    async def churn():
        for _ in range(20):
            a = await pool.select("gem")
            if a:
                await pool.release(a)
                await pool.record_success(a)
    async def snapshot():
        for _ in range(20):
            s = await pool.stats()
            assert "accounts_total" in s
    async def go():
        await asyncio.gather(*(churn() for _ in range(3)), snapshot())
    asyncio.run(go())


def test_recent_requests_evicted_after_window():
    """Old entries are popleft'd so the deque doesn't grow without bound."""
    pool = AccountPool()
    a = _acct("a", rate_limit_rpm=100)
    pool.add(a)
    # Plant 50 timestamps from 2 minutes ago — they should be evicted
    # on the next select, not counted against the rate limit.
    a.recent_requests.extend([time.time() - 120] * 50)
    chosen = asyncio.run(pool.select("gem"))
    assert chosen is a
    # The 50 stale entries are gone, only the freshly-appended one remains.
    assert len(a.recent_requests) == 1


def test_record_usage_appends_to_log():
    pool = AccountPool()
    a = _acct("a")
    pool.add(a)
    asyncio.run(pool.record_usage(a, "gem", prompt_tokens=10, completion_tokens=20, total_tokens=30))
    assert len(pool.usage_log) == 1
    tick = pool.usage_log[0]
    assert isinstance(tick, UsageTick)
    assert tick.prompt == 10
    assert tick.completion == 20
    assert tick.total == 30
    assert tick.success_count == 1
    assert tick.by_model["gem"]["count"] == 1
    assert tick.by_account["a"] == 1


def test_record_usage_buckets_by_minute():
    pool = AccountPool()
    a = _acct("a")
    pool.add(a)
    # Two calls in the same wall-clock minute should fold into one tick.
    asyncio.run(pool.record_usage(a, "gem", prompt_tokens=5, completion_tokens=5, total_tokens=10))
    asyncio.run(pool.record_usage(a, "gem", prompt_tokens=3, completion_tokens=2, total_tokens=5))
    assert len(pool.usage_log) == 1
    assert pool.usage_log[0].prompt == 8
    assert pool.usage_log[0].completion == 7


def test_usage_series_fills_empty_minutes():
    pool = AccountPool()
    a = _acct("a")
    pool.add(a)
    asyncio.run(pool.record_usage(a, "gem", prompt_tokens=1, completion_tokens=1, total_tokens=2))
    series = pool.usage_series(hours=1)
    # 1 hour = 60 minutes, with a single non-zero point.
    assert len(series) == 61
    non_zero = [p for p in series if p["total"] > 0]
    assert len(non_zero) == 1


def test_round_robin_walks_through_eligible_accounts():
    pool = AccountPool(strategy="round-robin")
    a, b, c = _acct("a"), _acct("b"), _acct("c")
    pool.add(a)
    pool.add(b)
    pool.add(c)
    picks = [asyncio.run(pool.select("gem")) for _ in range(3)]
    assert [p.name for p in picks] == ["a", "b", "c"]
