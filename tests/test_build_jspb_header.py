"""Tests for ``adapter.build_jspb_header``."""
from __future__ import annotations

import json

import pytest

from adapter import build_jspb_header


def test_streaming_header_shape():
    arr = json.loads(build_jspb_header(1, 2, "abc", "hash", for_stream=True))
    assert isinstance(arr, list)
    assert len(arr) == 17
    assert arr[14] == 1   # model_family
    assert arr[15] == 2   # thinking_mode
    assert arr[16] == "abc"  # session_uuid
    assert arr[4] == "hash"  # request_hash
    assert arr[8] == [4, 5, 6, 8]


def test_non_streaming_header_shape():
    arr = json.loads(build_jspb_header(6, 1, "uuid", "", for_stream=False))
    assert len(arr) == 17
    assert arr[14] == 6
    assert arr[15] == 1
    assert arr[16] == "uuid"
    # Non-streaming variant leaves request_hash slot empty.
    assert arr[4] is None


def test_missing_uuid_serialised_as_null():
    arr = json.loads(build_jspb_header(1, 1, "", "", for_stream=True))
    assert arr[16] is None
    assert arr[4] is None
