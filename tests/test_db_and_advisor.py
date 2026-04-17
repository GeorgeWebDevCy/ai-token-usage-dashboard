"""DB round-trip + advisor smoke tests."""

from __future__ import annotations

import time

import pytest

from ai_token_dashboard.advisor import generate
from ai_token_dashboard.db import Database
from ai_token_dashboard.events import TokenEvent


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "t.db")


def test_insert_and_query(db):
    now = time.time()
    e = TokenEvent(
        source="claude_code", model="claude-sonnet-4-6",
        input_tokens=100, output_tokens=50, cost_usd=0.001,
        timestamp=now, event_id="e1",
    )
    assert db.insert(e) is True
    # Duplicate event_id should be a no-op.
    assert db.insert(e) is False
    t = db.totals_since(now - 60)
    assert t["input_tokens"] == 100
    assert t["output_tokens"] == 50
    assert t["event_count"] == 1


def test_by_source_and_model(db):
    now = time.time()
    db.insert(TokenEvent(source="claude_code", model="claude-sonnet-4-6",
                         input_tokens=100, output_tokens=100, cost_usd=0.001,
                         timestamp=now, event_id="a"))
    db.insert(TokenEvent(source="codex", model="gpt-5",
                         input_tokens=50, output_tokens=50, cost_usd=0.0005,
                         timestamp=now, event_id="b"))
    by_src = db.by_source_since(now - 60)
    sources = {r["source"] for r in by_src}
    assert sources == {"claude_code", "codex"}
    by_model = db.by_model_since(now - 60)
    assert any(r["model"] == "claude-sonnet-4-6" for r in by_model)


def test_advisor_flags_expensive_small_output(db, tmp_path):
    """Seed 30 Opus turns with tiny outputs and verify the advisor spots it."""
    now = time.time()
    for i in range(30):
        db.insert(TokenEvent(
            source="claude_code", model="claude-opus-4-6",
            input_tokens=500, output_tokens=50,
            cost_usd=0.05, timestamp=now - i, event_id=f"opus-{i}",
        ))
    suggestions = generate(db.path, days=7)
    rule_ids = {s.rule_id for s in suggestions}
    assert "expensive_model_small_output" in rule_ids


def test_advisor_flags_low_cache_usage(db):
    now = time.time()
    for i in range(12):
        db.insert(TokenEvent(
            source="claude_code", model="claude-sonnet-4-6",
            input_tokens=6000, output_tokens=500,
            cache_read_tokens=0, cache_write_tokens=0,
            cost_usd=0.02, session_id="long-sess",
            timestamp=now - i, event_id=f"nocache-{i}",
        ))
    suggestions = generate(db.path, days=7)
    assert any(s.rule_id == "low_cache_usage" for s in suggestions)


def test_advisor_returns_empty_on_empty_db(db):
    assert generate(db.path, days=7) == []
