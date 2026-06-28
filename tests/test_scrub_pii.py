"""Tests for ``logger.scrub_pii``."""
from __future__ import annotations

import pytest

from logger import scrub_pii


def test_email_is_redacted():
    assert scrub_pii("contact me at alice@example.com please") == "contact me at <email> please"


def test_chinese_mobile_is_redacted():
    assert scrub_pii("call 13812345678 now") == "call <mobile> now"


def test_chinese_id_is_redacted():
    # A synthetic but well-formed 18-digit ID number.
    text = "ID 11010519491231002X expired"
    out = scrub_pii(text)
    assert "<id>" in out
    assert "11010519491231002X" not in out


def test_normal_text_unchanged():
    text = "Hello world, the year is 2026 and the model is great."
    assert scrub_pii(text) == text


def test_short_numbers_not_redacted():
    # Make sure the regex doesn't over-match on short digit runs.
    assert scrub_pii("price: 99, quantity: 5, code 42") == "price: 99, quantity: 5, code 42"


def test_empty_input():
    assert scrub_pii("") == ""
    assert scrub_pii("") == ""


def test_multiple_pii_in_one_line():
    text = "user=alice@example.com, phone=13812345678, id=11010519491231002X"
    out = scrub_pii(text)
    assert out == "user=<email>, phone=<mobile>, id=<id>"


def test_idempotent():
    text = "alice@example.com"
    once = scrub_pii(text)
    twice = scrub_pii(once)
    assert once == twice == "<email>"
