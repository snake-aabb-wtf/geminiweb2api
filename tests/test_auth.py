"""Tests for ``auth`` dependency functions."""
from __future__ import annotations

import importlib

import pytest
from fastapi import HTTPException


def _reload_with(monkeypatch, **env):
    """Reload ``auth`` with the given env vars in place."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import auth as auth_module
    importlib.reload(auth_module)
    return auth_module


def test_disabled_when_placeholder(monkeypatch):
    auth = _reload_with(monkeypatch, API_KEY="sk-web2api-placeholder")
    assert auth.CONFIG.api_required is False
    assert auth.CONFIG.admin_required is False


def test_enabled_when_real_key(monkeypatch):
    auth = _reload_with(monkeypatch, API_KEY="sk-real", ADMIN_KEY="sk-admin")
    assert auth.CONFIG.api_required is True
    assert auth.CONFIG.admin_required is True


def test_check_admin_login(monkeypatch):
    auth = _reload_with(monkeypatch, API_KEY="sk-real", ADMIN_KEY="sk-admin")
    assert auth.check_admin_login("sk-admin") is True
    assert auth.check_admin_login("wrong") is False
    assert auth.check_admin_login("") is False


def test_verify_api_key_allows_correct(monkeypatch):
    auth = _reload_with(monkeypatch, API_KEY="sk-real", ADMIN_KEY="sk-real")
    # Should not raise.
    auth.verify_api_key(request=_fake_request(), authorization="Bearer sk-real", x_api_key=None)


def test_verify_api_key_rejects_missing(monkeypatch):
    auth = _reload_with(monkeypatch, API_KEY="sk-real", ADMIN_KEY="sk-real")
    with pytest.raises(HTTPException) as exc:
        auth.verify_api_key(request=_fake_request(), authorization=None, x_api_key=None)
    assert exc.value.status_code == 401


def test_verify_api_key_rejects_wrong(monkeypatch):
    auth = _reload_with(monkeypatch, API_KEY="sk-real", ADMIN_KEY="sk-real")
    with pytest.raises(HTTPException) as exc:
        auth.verify_api_key(request=_fake_request(), authorization="Bearer wrong", x_api_key=None)
    assert exc.value.status_code == 401


def test_verify_api_key_disabled_noop(monkeypatch):
    auth = _reload_with(monkeypatch, API_KEY="")
    # Should not raise even with no headers.
    auth.verify_api_key(request=_fake_request(), authorization=None, x_api_key=None)


def _fake_request():
    class _Req:
        client = type("C", (), {"host": "127.0.0.1"})()
        url = type("U", (), {"path": "/v1/test"})()
        headers: dict = {}
    return _Req()
