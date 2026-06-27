"""Tests for ``adapter.parse_response``."""
from __future__ import annotations

import json
import textwrap

from adapter import parse_response


def _wrap(inner_list) -> str:
    """Build a minimal but well-formed Gemini response.

    The outer shape is the one ``stream_request``/``send_request`` actually
    see: a list of newline-delimited JSON objects, the first of which is
    the ``wrb`` frame containing the inner string we want to parse.
    """
    inner_str = json.dumps(inner_list, ensure_ascii=False, separators=(",", ":"))
    wrb = ["wrb.fr", None, inner_str, None]
    body = json.dumps([wrb], ensure_ascii=False, separators=(",", ":"))
    # Gemini prepends an XSSI guard and a digit-only line.
    return ")]}'\n42\n" + body + "\n"


def test_primary_answer_chosen_from_first_candidate():
    inner = [None, None, None, None,
             [["rc", ["primary answer", "extra"], None]],
             None]
    parsed = parse_response(_wrap(inner))
    assert parsed.content == "primary answerextra"
    assert parsed.usage == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def test_second_candidate_becomes_reasoning():
    inner = [None, None, None, None,
             [["rc", ["final answer"], None],
              ["rc", ["thinking process"], None]]]
    parsed = parse_response(_wrap(inner))
    assert parsed.content == "final answer"
    assert parsed.reasoning == "thinking process"


def test_token_usage_extracted_from_meta_block():
    inner = [None, None,
             {"_mtokenCount": 17, "_stokenCount": 3, "_ttokenCount": 25},
             None,
             [["rc", ["x"], None]]]
    parsed = parse_response(_wrap(inner))
    assert parsed.usage == {"prompt_tokens": 8, "completion_tokens": 17, "total_tokens": 25}


def test_tool_calls_detected_in_meta_slot():
    inner = [None, None, None, None,
             [["rc", ["answer"], None,
               [{"name": "get_weather", "arguments": {"city": "Shanghai"}}]]]]
    parsed = parse_response(_wrap(inner))
    assert len(parsed.tool_calls) == 1
    assert parsed.tool_calls[0]["function"]["name"] == "get_weather"


def test_garbage_lines_are_skipped():
    parsed = parse_response(")\nnot json\n42\n{}\n[1, 2, 3]\n")
    # 1, 2, 3 is short — wrb[2] would have to be a string. We just check
    # we don't crash and we return a ParsedResponse.
    assert parsed.content == ""


def test_empty_input():
    parsed = parse_response("")
    assert parsed.content == ""
    assert parsed.reasoning == ""
    assert parsed.tool_calls == []
