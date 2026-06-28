"""Shared pytest fixtures and path setup."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

# Make the project root importable for `import server`, `import adapter`, etc.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _reload_auth_per_test(request):
    """Reload ``auth`` after every test so its module-level CONFIG is
    re-evaluated against the current environment.

    Several tests in this repo deliberately ``monkeypatch.setenv`` keys
    and call ``importlib.reload(auth)`` to flip auth on/off. If the
    next test forgets to reload auth, the previous test's CONFIG would
    leak in. The autouse fixture guarantees a clean slate.
    """
    yield
    # Only reload after tests that touched ``auth`` (heuristic: any test
    # that imported auth or server directly).
    if any("auth" in str(item.fixturenames) or "server" in str(item.fixturenames)
           for item in [request.node]):
        import auth
        importlib.reload(auth)
