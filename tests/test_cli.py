"""CLI subcommand tests."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ai_token_dashboard import cli
from ai_token_dashboard.config import AppConfig
from ai_token_dashboard.db import Database
from ai_token_dashboard.events import TokenEvent


@pytest.fixture()
def cfg_with_db(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    return AppConfig.load(cfg_path), cfg_path


def test_today_prints(capsys, cfg_with_db):
    cfg, _ = cfg_with_db
    # Point DB into tmp so we don't clobber real data.
    cfg.db_path = Path(cfg.db_path).parent / "cli-today.db"
    db = Database(cfg.db_path)
    db.insert(TokenEvent(source="claude_code", model="claude-sonnet-4-6",
                         input_tokens=100, output_tokens=50, cost_usd=0.01,
                         timestamp=time.time(), event_id="t1"))
    assert cli.cmd_today(cfg) == 0
    out = capsys.readouterr().out
    assert "Today" in out
    assert "Assistant turns:" in out
    assert "claude-sonnet-4-6" in out


def test_stats_prints_with_suggestions(capsys, tmp_path, cfg_with_db):
    cfg, _ = cfg_with_db
    cfg.db_path = tmp_path / "cli-stats.db"
    db = Database(cfg.db_path)
    now = time.time()
    for i in range(25):
        db.insert(TokenEvent(
            source="claude_code", model="claude-opus-4-6",
            input_tokens=500, output_tokens=50, cost_usd=0.05,
            timestamp=now - i, event_id=f"s{i}",
        ))
    assert cli.cmd_stats(cfg, days=7) == 0
    out = capsys.readouterr().out
    assert "Last 7 days" in out
    assert "Top suggestions" in out


def test_scan_ingests_existing_jsonl(capsys, tmp_path, cfg_with_db):
    cfg, _ = cfg_with_db
    cfg.db_path = tmp_path / "cli-scan.db"
    # Point Claude paths at a fixture dir with one JSONL file.
    fixture_dir = tmp_path / "fake_claude"
    fixture_dir.mkdir()
    jsonl = fixture_dir / "session.jsonl"
    jsonl.write_text(json.dumps({
        "type": "assistant",
        "uuid": "scan-test-1",
        "sessionId": "s1",
        "message": {
            "model": "claude-sonnet-4-6",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    }) + "\n")
    cfg.collectors["claude_code"].paths = [str(fixture_dir)]
    # Disable other collectors.
    for name in ("codex", "gemini"):
        if name in cfg.collectors:
            cfg.collectors[name].enabled = False

    assert cli.cmd_scan(cfg) == 0
    out = capsys.readouterr().out
    assert "scanning" in out
    assert "Inserted 1 new events" in out


def test_dispatch_returns_minus_one_for_daemon(tmp_path):
    # No args and daemon-only flags should not be routed to CLI.
    assert cli.dispatch([]) == -1
    assert cli.dispatch(["--no-tray"]) == -1
    assert cli.dispatch(["--open"]) == -1


def test_dispatch_routes_subcommands(tmp_path):
    # 'today' should be handled by CLI (returns 0 or error, not -1).
    rc = cli.dispatch(["today", "--config", str(tmp_path / "c.yaml")])
    assert rc == 0
