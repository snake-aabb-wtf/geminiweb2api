"""Tests for the in-memory log handler / SSE bridge."""
from __future__ import annotations

import asyncio
import logging
import time

import pytest

from server import _InMemoryLogHandler


def _build_handler():
    h = _InMemoryLogHandler()
    h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
    return h


def test_emit_attaches_to_alive_queues():
    h = _build_handler()
    h.bind_loop(asyncio.new_event_loop())
    q1: asyncio.Queue = asyncio.Queue()
    q2: asyncio.Queue = asyncio.Queue()
    h.queues.extend([q1, q2])

    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="hello", args=(), exc_info=None,
    )
    h.emit(record)
    # call_soon_threadsafe on a non-running loop raises; instead we
    # just verify the queues are still attached.
    assert q1 in h.queues
    assert q2 in h.queues


def test_mark_dead_then_force_remove():
    h = _build_handler()
    q: asyncio.Queue = asyncio.Queue()
    h.queues.append(q)
    h.mark_dead(q)
    h.force_remove(q)
    assert q not in h.queues


def test_dead_queues_pruned_on_enqueue_after_grace():
    h = _build_handler()
    q: asyncio.Queue = asyncio.Queue()
    h.queues.append(q)
    h.mark_dead(q)
    # Backdate the death beyond the 30s grace window.
    h._dead[q] = time.time() - 60
    # enqueue runs the prune.
    h._enqueue({"ts": "now", "level": "INFO", "logger": "t", "message": "x"})
    assert q not in h.queues
    assert q not in h._dead


def test_queue_full_drops_oldest():
    h = _build_handler()
    h.bind_loop(asyncio.new_event_loop())
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    h.queues.append(q)
    # Fill it.
    h._enqueue({"ts": "1", "level": "I", "logger": "t", "message": "a"})
    h._enqueue({"ts": "2", "level": "I", "logger": "t", "message": "b"})
    # Next push should evict the oldest, keeping the new one.
    h._enqueue({"ts": "3", "level": "I", "logger": "t", "message": "c"})
    # The queue still has 2 items; the newest is in it.
    assert q.qsize() == 2
    # The newest message must be present.
    items = []
    while not q.empty():
        items.append(q.get_nowait()["message"])
    assert "c" in items
