"""Structured logging for gemini2api.

Provides a single ``get_logger()`` factory that returns a logger writing to
both stderr (configurable level) and a rotating file ``gemini_proxy.log``.

Key events emitted by the rest of the codebase:
    - request_started / request_completed
    - upstream_error
    - account_disabled / account_enabled
    - api_key_invalid
    - rate_limited

Design note: any ``extra={...}`` passed to a log call is namespace-prefixed
with ``event_`` to avoid colliding with reserved LogRecord attributes
(``name``, ``msg``, ``args``, …). Callers can therefore use natural keys
like ``extra={"name": "..."}`` without worrying about clashes.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Any, Optional

# LogRecord reserves these — we never want to let user `extra` shadow them.
_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime",
}


def _scrub_extra(extra: Optional[dict]) -> dict:
    """Rename any reserved keys to ``event_<name>`` so ``makeRecord`` is happy."""
    if not extra:
        return {}
    out: dict[str, Any] = {}
    for k, v in extra.items():
        if k in _RESERVED:
            out[f"event_{k}"] = v
        else:
            out[k] = v
    return out


class _SafeLogger(logging.LoggerAdapter):
    """LoggerAdapter that scrubs ``extra`` of reserved keys before delegating."""

    def process(self, msg, kwargs):  # type: ignore[override]
        kwargs["extra"] = {**self.extra, **_scrub_extra(kwargs.get("extra"))}
        return msg, kwargs


_LOG_FORMAT = (
    "%(asctime)s | %(levelname)-7s | %(name)s | "
    "%(filename)s:%(lineno)d | %(message)s"
)
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_initialised = False


def _initialise() -> None:
    """Idempotently configure the root gemini2api logger."""
    global _initialised
    if _initialised:
        return

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger("gemini2api")
    root.setLevel(level)
    root.propagate = False  # Don't bubble up to the root logger

    # Avoid double-adding handlers if called twice (e.g. in tests).
    if root.handlers:
        _initialised = True
        return

    formatter = logging.Formatter(_LOG_FORMAT, _DATE_FORMAT)

    # Stderr handler — always present, respects LOG_LEVEL.
    stream = logging.StreamHandler(stream=sys.stderr)
    stream.setFormatter(formatter)
    stream.setLevel(level)
    root.addHandler(stream)

    # File handler — best-effort, falls back gracefully when the cwd is not
    # writable (e.g. inside a frozen exe or read-only FS).
    log_file = os.getenv("LOG_FILE", "gemini_proxy.log")
    try:
        log_path = Path(log_file)
        if not log_path.is_absolute():
            log_path = Path.cwd() / log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        rotating.setFormatter(formatter)
        rotating.setLevel(level)
        root.addHandler(rotating)
    except OSError:
        # Filesystem not writable; stderr already covers us.
        pass

    _initialised = True


def get_logger(name: Optional[str] = None) -> _SafeLogger:
    """Return a configured logger adapter under the ``gemini2api`` namespace.

    The returned object supports the standard ``.info(msg, extra=...)``
    pattern. Any reserved key in ``extra`` is silently rewritten to
    ``event_<name>`` so it never collides with LogRecord's built-in fields.
    """
    _initialise()
    if name and not name.startswith("gemini2api"):
        name = f"gemini2api.{name}"
    base = logging.getLogger(name or "gemini2api")
    return _SafeLogger(base, {})
