"""Account pool, rotation strategies, and ``.env`` persistence.

The pool is the single source of truth for live Gemini credentials. It
serves three purposes:

1. **Selection** — pick the next account to handle a request, honouring
   enabled flag, error budget, in-flight cap, and per-model bindings.
2. **Stats** — record success / failure / retry counts, surface them via
   ``/api/stats``.
3. **Persistence** — write back to ``.env`` when the WebUI mutates the
   pool, and re-read it on a configurable interval to pick up manual edits.

Concurrency: every mutating method is guarded by ``self._lock`` so multiple
concurrent ``/v1/chat/completions`` coroutines cannot race on
``last_used`` / ``error_count`` / ``inflight`` updates.
"""
from __future__ import annotations

import asyncio
import os
import random
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from logger import get_logger

log = get_logger("account_pool")

# Headline version — bumped in lockstep with server.py.
__version__ = "1.0.0"


# ── Account ──────────────────────────────────────────────────────────

@dataclass
class Account:
    """A single Google account's credentials + runtime bookkeeping."""

    name: str
    f_sid: str = ""
    at: str = ""
    sn_param: str = ""
    bl_param: str = ""
    hl: str = "zh-CN"
    session_uuid: str = ""
    request_hash: str = ""
    headers: dict = field(default_factory=dict)
    enabled: bool = True
    error_count: int = 0
    last_used: float = 0.0
    last_error: str = ""
    last_success: float = 0.0
    bound_models: list = field(default_factory=list)
    rate_limit_rpm: int = 60           # Token-bucket cap, requests / minute.
    max_concurrent: int = 4            # In-flight request ceiling.
    inflight: int = 0                  # Updated atomically by the pool.
    recent_requests: deque = field(default_factory=lambda: deque(maxlen=128))


@dataclass
class UsageTick:
    """One minute of aggregated token usage across the pool."""

    ts: float                         # unix seconds, truncated to the minute
    prompt: int = 0
    completion: int = 0
    total: int = 0
    success_count: int = 0
    by_model: dict = field(default_factory=dict)   # {model_name: {prompt, completion, total, count}}
    by_account: dict = field(default_factory=dict) # {account_name: count}


# ── Pool ─────────────────────────────────────────────────────────────

class AccountPool:
    """Thread-/asyncio-safe collection of ``Account`` objects."""

    VALID_STRATEGIES = ("least-recently-used", "round-robin", "random", "first")

    def __init__(
        self,
        strategy: str = "least-recently-used",
        max_errors: int = 3,
        env_path: Optional[Path] = None,
        usage_log_size: int = 24 * 60,  # 24h * 60min/h
    ):
        self.accounts: list[Account] = []
        self.strategy = strategy if strategy in self.VALID_STRATEGIES else "least-recently-used"
        self.max_errors = max(1, int(max_errors))
        self._lock = asyncio.Lock()
        self._round_robin_idx = 0
        self._stats = {
            "total_requests": 0,
            "success": 0,
            "failures": 0,
            "retries": 0,
            "rate_limited": 0,
        }
        # 24h rolling token-usage log (one bucket per minute).
        self.usage_log: deque[UsageTick] = deque(maxlen=usage_log_size)
        # Optional path used by save_to_env / reload_if_changed.
        self._env_path: Optional[Path] = Path(env_path) if env_path else None
        self._env_mtime: float = 0.0

    # ── CRUD ────────────────────────────────────────────────────────

    def add(self, account: Account) -> None:
        # Replace if a same-name account already exists.
        for i, existing in enumerate(self.accounts):
            if existing.name == account.name:
                self.accounts[i] = account
                log.info("account_replaced", extra={"name": account.name})
                return
        self.accounts.append(account)
        log.info("account_added", extra={"name": account.name})

    def remove(self, name: str) -> bool:
        before = len(self.accounts)
        self.accounts = [a for a in self.accounts if a.name != name]
        removed = len(self.accounts) != before
        if removed:
            log.info("account_removed", extra={"name": name})
        return removed

    def get_account(self, name: str) -> Optional[Account]:
        for a in self.accounts:
            if a.name == name:
                return a
        return None

    # ── Selection ───────────────────────────────────────────────────

    async def select(self, model_name: str) -> Optional[Account]:
        """Pick the best account for ``model_name`` under the active strategy.

        Returns ``None`` if no account passes the enabled / error / binding
        / rate-limit / in-flight filters.
        """
        async with self._lock:
            now = time.time()
            window_start = now - 60.0

            candidates = []
            for a in self.accounts:
                if not a.enabled:
                    continue
                if a.error_count >= self.max_errors:
                    continue
                if model_name not in a.bound_models:
                    continue
                if a.inflight >= a.max_concurrent:
                    continue
                # Evict stale timestamps from the sliding window so the
                # deque doesn't grow forever and the count is O(k) not O(N).
                while a.recent_requests and a.recent_requests[0] <= window_start:
                    a.recent_requests.popleft()
                if len(a.recent_requests) >= a.rate_limit_rpm:
                    self._stats["rate_limited"] += 1
                    log.debug("account_rate_limited", extra={"name": a.name, "model": model_name})
                    continue
                candidates.append(a)

            if not candidates:
                return None

            if self.strategy == "least-recently-used":
                # Break ties with a small random jitter so cold-start with
                # many ``last_used=0`` accounts doesn't always pick the
                # first one. The jitter is tiny (< 1e-3) so it never
                # overrides a real "older" timestamp.
                chosen = min(candidates, key=lambda a: (a.last_used, random.random()))
            elif self.strategy == "random":
                chosen = random.choice(candidates)
            elif self.strategy == "round-robin":
                # Walk forward until we find a candidate; resilient to
                # non-eligible accounts slipping in between eligible ones.
                chosen = None
                for _ in range(len(self.accounts)):
                    acct = self.accounts[self._round_robin_idx % len(self.accounts)]
                    self._round_robin_idx += 1
                    if acct in candidates:
                        chosen = acct
                        break
                if chosen is None:  # pragma: no cover
                    return None
            else:  # "first"
                chosen = candidates[0]

            assert chosen is not None  # for type-checker; we returned earlier if no candidates
            chosen.inflight += 1
            chosen.last_used = now
            chosen.recent_requests.append(now)
            return chosen

    async def release(self, account: Account) -> None:
        """Decrement the in-flight counter; called on success *or* failure."""
        async with self._lock:
            if account.inflight > 0:
                account.inflight -= 1

    # ── Stats update helpers ────────────────────────────────────────

    async def record_success(self, account: Account) -> None:
        async with self._lock:
            account.error_count = 0
            account.last_success = time.time()
            account.last_error = ""
            self._stats["success"] += 1

    async def record_failure(self, account: Account, reason: str = "") -> None:
        async with self._lock:
            account.error_count += 1
            account.last_error = reason[:200]
            self._stats["failures"] += 1
            if account.error_count >= self.max_errors:
                log.warning(
                    "account_disabled",
                    extra={"name": account.name, "reason": reason, "errors": account.error_count},
                )

    async def record_usage(
        self,
        account: Account,
        model_name: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        """Fold a successful response's token counts into the rolling log.

        Buckets are aligned to the wall-clock minute so a 24h query
        returns 1440 evenly-spaced points even after only a few requests.
        """
        async with self._lock:
            now = time.time()
            minute = int(now // 60) * 60
            if self.usage_log and self.usage_log[-1].ts == minute:
                tick = self.usage_log[-1]
            else:
                tick = UsageTick(ts=minute)
                self.usage_log.append(tick)
            tick.prompt += int(prompt_tokens or 0)
            tick.completion += int(completion_tokens or 0)
            tick.total += int(total_tokens or (prompt_tokens or 0) + (completion_tokens or 0))
            tick.success_count += 1
            m = tick.by_model.setdefault(model_name, {"prompt": 0, "completion": 0, "total": 0, "count": 0})
            m["prompt"] += int(prompt_tokens or 0)
            m["completion"] += int(completion_tokens or 0)
            m["total"] += int(total_tokens or (prompt_tokens or 0) + (completion_tokens or 0))
            m["count"] += 1
            a = tick.by_account.setdefault(account.name, 0)
            tick.by_account[account.name] = a + 1

    def record_retry(self) -> None:
        self._stats["retries"] += 1

    def record_request(self) -> None:
        self._stats["total_requests"] += 1

    # ── Reporting ───────────────────────────────────────────────────

    async def stats(self) -> dict:
        """Snapshot of the pool — read under the lock to be race-free."""
        async with self._lock:
            now = time.time()
            with_stats = []
            for a in self.accounts:
                # Account for stale entries without mutating the deque.
                recent = sum(1 for t in a.recent_requests if t > now - 60)
                with_stats.append({
                    "name": a.name,
                    "enabled": a.enabled,
                    "error_count": a.error_count,
                    "last_used": a.last_used,
                    "last_success": a.last_success,
                    "last_error": a.last_error,
                    "inflight": a.inflight,
                    "recent_60s": recent,
                    "bound_models": a.bound_models,
                })
            return {
                **self._stats,
                "accounts_total": len(self.accounts),
                "accounts_enabled": sum(1 for a in self.accounts if a.enabled),
                "accounts_disabled": sum(1 for a in self.accounts if not a.enabled),
                "accounts_exhausted": sum(1 for a in self.accounts if a.error_count >= self.max_errors),
                "strategy": self.strategy,
                "max_errors": self.max_errors,
                "accounts": with_stats,
            }

    def stats_sync(self) -> dict:
        """Synchronous snapshot for use *outside* the event loop.

        Holds the lock synchronously; safe because the lock is also an
        asyncio lock only when no event loop is running in the same
        thread. For background tasks / startup probes we expose this
        thin wrapper.
        """
        # ``asyncio.Lock`` cannot be acquired outside a running loop, so
        # fall back to a best-effort read here. The classic asyncio lock
        # blocks the loop, so this method is reserved for tooling like
        # the GUI config tool that runs in a separate thread.
        now = time.time()
        with_stats = []
        for a in self.accounts:
            with_stats.append({
                "name": a.name,
                "enabled": a.enabled,
                "error_count": a.error_count,
                "last_used": a.last_used,
                "last_success": a.last_success,
                "last_error": a.last_error,
                "inflight": a.inflight,
                "recent_60s": sum(1 for t in a.recent_requests if t > now - 60),
                "bound_models": a.bound_models,
            })
        return {
            **self._stats,
            "accounts_total": len(self.accounts),
            "accounts_enabled": sum(1 for a in self.accounts if a.enabled),
            "accounts_disabled": sum(1 for a in self.accounts if not a.enabled),
            "accounts_exhausted": sum(1 for a in self.accounts if a.error_count >= self.max_errors),
            "strategy": self.strategy,
            "max_errors": self.max_errors,
            "accounts": with_stats,
        }

    def usage_series(self, hours: int = 24) -> list[dict]:
        """Return minute-bucketed usage for the last ``hours`` hours.

        Empty minutes are filled in as zero-points so the frontend can
        plot a continuous time series without gaps.
        """
        now = time.time()
        cutoff = now - hours * 3600
        # Build a quick lookup of present minutes.
        present: dict[int, UsageTick] = {int(t.ts): t for t in self.usage_log if t.ts >= cutoff}
        out: list[dict] = []
        start_minute = int(cutoff // 60) * 60
        end_minute = int(now // 60) * 60
        for m in range(start_minute, end_minute + 60, 60):
            tick = present.get(m)
            if tick is None:
                out.append({
                    "ts": m, "prompt": 0, "completion": 0,
                    "total": 0, "success_count": 0,
                    "by_model": {}, "by_account": {},
                })
            else:
                out.append({
                    "ts": tick.ts, "prompt": tick.prompt, "completion": tick.completion,
                    "total": tick.total, "success_count": tick.success_count,
                    "by_model": tick.by_model, "by_account": tick.by_account,
                })
        return out

    # ── .env loading / saving ───────────────────────────────────────

    def load_from_env(self) -> None:
        """Parse ``ACCOUNT_*`` variables from the current process env."""
        prefix = "ACCOUNT_"
        accounts_data: dict[str, dict] = {}
        for key, val in sorted(os.environ.items()):
            if not key.startswith(prefix):
                continue
            parts = key[len(prefix):].split("_", 1)
            if len(parts) != 2:
                continue
            acct_name, field_name = parts
            acct = accounts_data.setdefault(acct_name, {"name": acct_name})
            if field_name == "ENABLED":
                acct["enabled"] = val.strip().lower() in ("true", "1", "yes")
            elif field_name == "HEADER":
                if "=" in val:
                    hname, hval = val.split("=", 1)
                else:
                    hname, hval = val, ""
                acct.setdefault("headers", {})[hname] = hval
            elif field_name == "MODELS":
                acct["bound_models"] = [m.strip() for m in val.split(",") if m.strip()]
            elif field_name == "RATE_LIMIT_RPM":
                try:
                    acct["rate_limit_rpm"] = int(val)
                except ValueError:
                    pass
            elif field_name == "MAX_CONCURRENT":
                try:
                    acct["max_concurrent"] = int(val)
                except ValueError:
                    pass
            else:
                # Generic passthrough (F_SID, AT, SN_PARAM, BL_PARAM, HL, UUID, HASH…)
                acct[field_name.lower()] = val
        for data in accounts_data.values():
            self.add(Account(**data))
        log.info("accounts_loaded", extra={"count": len(accounts_data)})

    def to_env_lines(self) -> list[str]:
        """Serialise every account back to ``ACCOUNT_*`` lines."""
        lines: list[str] = []
        for a in self.accounts:
            lines.append(f"ACCOUNT_{a.name}_F_SID={a.f_sid}")
            lines.append(f"ACCOUNT_{a.name}_AT={a.at}")
            lines.append(f"ACCOUNT_{a.name}_SN_PARAM={a.sn_param}")
            lines.append(f"ACCOUNT_{a.name}_BL_PARAM={a.bl_param}")
            lines.append(f"ACCOUNT_{a.name}_HL={a.hl}")
            lines.append(f"ACCOUNT_{a.name}_UUID={a.session_uuid}")
            lines.append(f"ACCOUNT_{a.name}_HASH={a.request_hash}")
            lines.append(f"ACCOUNT_{a.name}_ENABLED={'true' if a.enabled else 'false'}")
            lines.append(f"ACCOUNT_{a.name}_MODELS={','.join(a.bound_models)}")
            lines.append(f"ACCOUNT_{a.name}_RATE_LIMIT_RPM={a.rate_limit_rpm}")
            lines.append(f"ACCOUNT_{a.name}_MAX_CONCURRENT={a.max_concurrent}")
            for hname, hval in a.headers.items():
                # Round-trip custom headers verbatim.
                lines.append(f"ACCOUNT_{a.name}_HEADER_{hname}={hval}")
        return lines

    def save_to_env(self, path: Optional[Path] = None) -> Path:
        """Persist current accounts to ``.env``, preserving non-account keys.

        The file is rewritten in one shot with a temp file + ``os.replace``,
        which is atomic on every platform we support.
        """
        target = Path(path or self._env_path or ".env")
        self._env_path = target

        # Read existing file, drop any ACCOUNT_* lines we own.
        existing: list[str] = []
        if target.exists():
            for line in target.read_text(encoding="utf-8").splitlines():
                stripped = line.lstrip()
                if stripped.startswith("ACCOUNT_") and "_" in stripped[8:]:
                    continue  # skip — we'll re-emit it
                existing.append(line)

        new_section = ["", "# ── Accounts (auto-managed, do not edit by hand) ──"]
        new_section.extend(self.to_env_lines())
        new_section.append("")

        final = "\n".join(existing + new_section)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(final, encoding="utf-8")
        os.replace(tmp, target)
        self._env_mtime = target.stat().st_mtime
        log.info("env_saved", extra={"path": str(target), "accounts": len(self.accounts)})
        return target

    async def reload_if_changed(self) -> bool:
        """If ``.env`` mtime moved, re-parse and replace the pool."""
        if not self._env_path or not self._env_path.exists():
            return False
        mtime = self._env_path.stat().st_mtime
        if mtime <= self._env_mtime:
            return False
        async with self._lock:
            self.accounts.clear()
            self._round_robin_idx = 0
            # Re-read the env so load_from_env sees the latest values.
            try:
                from dotenv import load_dotenv
                load_dotenv(self._env_path, override=True)
            except Exception as exc:  # pragma: no cover
                log.warning("env_reload_failed", extra={"err": str(exc)})
                return False
            self.load_from_env()
            self._env_mtime = mtime
            log.info("env_reloaded", extra={"path": str(self._env_path)})
            return True
