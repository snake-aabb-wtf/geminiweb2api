"""Tests that per-request ``temperature`` / ``max_tokens`` do not
mutate the shared ``ModelProfile``."""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import dotenv
import httpx
import pytest


def _reload_server(env_path: Path):
    dotenv.load_dotenv(env_path, override=True)
    import server
    importlib.reload(server)
    return server


@pytest.fixture
def server_module(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("PROFILES=g\nDEFAULT_MODEL=g\n", encoding="utf-8")
    monkeypatch.setenv("ENV_PATH", str(env))
    for var in ("API_KEY", "ADMIN_KEY", "API_KEYS", "ADMIN_KEYS"):
        monkeypatch.delenv(var, raising=False)
    return _reload_server(env)


def test_profile_temperature_unchanged_after_requests(server_module, monkeypatch):
    """Send requests with different temperatures; the profile's own
    temperature must remain at its original value (or None)."""
    original_temperature = server_module.profiles["g"].temperature
    # Swap in a fake send_request so we never hit Google.
    seen_temperatures: list = []

    async def fake_send(profile, account, messages, reqid, tools=None, attachments=None):
        seen_temperatures.append(profile.temperature)
        return {
            "id": "x", "object": "chat.completion", "created": 0, "model": "g",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }, reqid + 1, 200

    from account_pool import Account
    server_module.pool.add(Account(
        name="a", f_sid="s", at="t", sn_param="n", bl_param="b", bound_models=["g"],
    ))

    server_module.send_request = fake_send  # type: ignore[assignment]
    # server.py now calls ``adapter.send_request``; mirror the patch at
    # the module level so the test's fake is actually invoked.
    monkeypatch.setattr("adapter.send_request", fake_send)

    async def go():
        for temp in (0.1, 0.5, 0.9):
            tr = httpx.ASGITransport(app=server_module.app)
            async with httpx.AsyncClient(transport=tr, base_url="http://t") as client:
                await client.post(
                    "/v1/chat/completions",
                    json={"model": "g", "temperature": temp,
                          "messages": [{"role": "user", "content": "x"}]},
                )

    asyncio.run(go())

    # The per-request calls saw the overridden temperatures…
    assert seen_temperatures == [0.1, 0.5, 0.9]
    # …but the shared profile was never mutated.
    assert server_module.profiles["g"].temperature == original_temperature
