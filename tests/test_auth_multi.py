"""Tests for multi-API-key support in ``auth``."""
from __future__ import annotations

import importlib

import pytest
from fastapi import HTTPException


def _reload(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import auth
    importlib.reload(auth)
    return auth


def _fake_request():
    class _Req:
        client = type("C", (), {"host": "127.0.0.1"})()
        url = type("U", (), {"path": "/v1/x"})()
        headers: dict = {}
    return _Req()


def test_api_keys_comma_separated(monkeypatch):
    auth = _reload(monkeypatch, API_KEYS="sk-a,sk-b,sk-c")
    assert auth.CONFIG.api_required is True
    assert auth.CONFIG.api_key_count == 3
    # Any of the three should be accepted.
    auth.verify_api_key(request=_fake_request(), authorization="Bearer sk-a", x_api_key=None)
    auth.verify_api_key(request=_fake_request(), authorization="Bearer sk-b", x_api_key=None)
    auth.verify_api_key(request=_fake_request(), authorization="Bearer sk-c", x_api_key=None)


def test_api_key_and_api_keys_both_accepted(monkeypatch):
    auth = _reload(monkeypatch, API_KEY="sk-solo", API_KEYS="sk-a,sk-b")
    assert auth.CONFIG.api_key_count == 3
    # All three should validate.
    for k in ("sk-solo", "sk-a", "sk-b"):
        auth.verify_api_key(request=_fake_request(), authorization=f"Bearer {k}", x_api_key=None)


def test_unlisted_key_rejected(monkeypatch):
    auth = _reload(monkeypatch, API_KEYS="sk-a,sk-b")
    with pytest.raises(HTTPException) as exc:
        auth.verify_api_key(request=_fake_request(), authorization="Bearer sk-bad", x_api_key=None)
    assert exc.value.status_code == 401


def test_placeholder_disables_multi_key(monkeypatch):
    auth = _reload(monkeypatch, API_KEYS="sk-web2api-placeholder,off")
    assert auth.CONFIG.api_required is False


def test_admin_keys_comma_separated(monkeypatch):
    auth = _reload(monkeypatch, ADMIN_KEYS="adm-1,adm-2")
    assert auth.CONFIG.admin_required is True
    assert auth.CONFIG.admin_key_count == 2
    assert auth.check_admin_login("adm-1") is True
    assert auth.check_admin_login("adm-2") is True
    assert auth.check_admin_login("adm-3") is False


def test_auth_summary_exposes_key_counts(monkeypatch):
    auth = _reload(monkeypatch, API_KEYS="a,b,c", ADMIN_KEYS="x,y")
    summary = auth.auth_summary()
    assert summary["api_key_count"] == 3
    assert summary["admin_key_count"] == 2
    assert summary["api_key_status"] == "required"
    assert summary["admin_key_status"] == "required"
