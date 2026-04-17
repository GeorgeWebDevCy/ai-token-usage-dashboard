"""Parser tests for each collector.

We instantiate the collector with a throwaway DB + dummy loop just to access
``parse_line``; we don't actually start watchdog here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_token_dashboard.collectors.claude_code import ClaudeCodeCollector
from ai_token_dashboard.collectors.codex import CodexCollector
from ai_token_dashboard.collectors.gemini import GeminiCollector


class _Stub:
    """Stand-in for Database+loop when we only care about parse_line."""
    def insert(self, ev): return True


def _make(cls):
    return cls(paths=[], db=_Stub(), loop=None)  # type: ignore[arg-type]


# ---- Claude Code --------------------------------------------------------

def test_claude_code_assistant_line_parses_usage():
    line = json.dumps({
        "type": "assistant",
        "uuid": "abc-123",
        "sessionId": "sess-xyz",
        "timestamp": "2026-04-17T10:00:00Z",
        "message": {
            "id": "msg_1",
            "model": "claude-sonnet-4-6",
            "usage": {
                "input_tokens": 120,
                "output_tokens": 340,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 2000,
            },
        },
    })
    coll = _make(ClaudeCodeCollector)
    events = list(coll.parse_line(line, Path("/fake.jsonl")))
    assert len(events) == 1
    ev = events[0]
    assert ev.source == "claude_code"
    assert ev.model == "claude-sonnet-4-6"
    assert ev.input_tokens == 120
    assert ev.output_tokens == 340
    assert ev.cache_read_tokens == 2000
    assert ev.session_id == "sess-xyz"
    assert ev.event_id == "claude:abc-123"
    assert ev.cost_usd > 0  # cost_for(claude-sonnet-4-6,...) > 0


def test_claude_code_non_assistant_line_emits_nothing():
    line = json.dumps({"type": "user", "message": {"content": "hi"}})
    coll = _make(ClaudeCodeCollector)
    assert list(coll.parse_line(line, Path("/fake.jsonl"))) == []


def test_claude_code_malformed_json_is_silently_ignored():
    coll = _make(ClaudeCodeCollector)
    assert list(coll.parse_line("{not json", Path("/fake.jsonl"))) == []


# ---- Codex --------------------------------------------------------------

def test_codex_modern_shape():
    line = json.dumps({
        "id": "resp_42",
        "type": "response_item",
        "response": {
            "model": "gpt-5",
            "usage": {"input_tokens": 50, "output_tokens": 80},
        },
    })
    coll = _make(CodexCollector)
    events = list(coll.parse_line(line, Path("/f.jsonl")))
    assert len(events) == 1
    assert events[0].model == "gpt-5"
    assert events[0].input_tokens == 50
    assert events[0].output_tokens == 80


def test_codex_legacy_prompt_completion_shape():
    line = json.dumps({
        "id": "r2",
        "model": "gpt-5-mini",
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    })
    coll = _make(CodexCollector)
    events = list(coll.parse_line(line, Path("/f.jsonl")))
    assert len(events) == 1
    assert events[0].input_tokens == 10
    assert events[0].output_tokens == 20


# ---- Gemini -------------------------------------------------------------

def test_gemini_usage_metadata_shape():
    line = json.dumps({
        "id": "g1",
        "model": "gemini-2.5-flash",
        "response": {
            "model": "gemini-2.5-flash",
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 200,
                "cachedContentTokenCount": 50,
            },
        },
    })
    coll = _make(GeminiCollector)
    events = list(coll.parse_line(line, Path("/f.jsonl")))
    assert len(events) == 1
    ev = events[0]
    assert ev.source == "gemini"
    assert ev.input_tokens == 100
    assert ev.output_tokens == 200
    assert ev.cache_read_tokens == 50
