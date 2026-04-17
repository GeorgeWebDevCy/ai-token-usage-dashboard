"""Claude Code session collector.

Claude Code (the CLI) appends each turn to ``~/.claude/projects/<slug>/<uuid>.jsonl``.
Each line is a JSON object. Assistant turns have ``message.usage`` with token
counts. The structure has shifted a little across versions, so we're generous
about where we look.

Representative shapes we handle:

    {"type":"assistant","message":{"id":"msg_...","model":"claude-sonnet-4-6",
        "usage":{"input_tokens":12,"output_tokens":34,
                 "cache_creation_input_tokens":0,"cache_read_input_tokens":0}},
     "sessionId":"...","timestamp":"2026-04-17T10:00:00Z","uuid":"..."}

    {"event":"message_end","usage":{...},"model":"...","session_id":"..."}
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

from ..events import TokenEvent
from ..pricing import cost_for
from .base import JsonlTailCollector, try_json


def _ts_of(obj: dict) -> float:
    raw = obj.get("timestamp") or obj.get("created_at")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    import time
    return time.time()


def _stable_id(obj: dict, path: Path) -> str:
    """A deterministic id so re-reads of the same line don't double-insert."""
    for key in ("uuid", "id", "message_id"):
        if obj.get(key):
            return f"claude:{obj[key]}"
    msg = obj.get("message") or {}
    for key in ("id", "uuid"):
        if msg.get(key):
            return f"claude:{msg[key]}"
    # Last-resort hash on line content + file.
    import hashlib
    h = hashlib.sha1(
        (str(path) + "|" + (obj.get("timestamp") or "") + "|" + str(obj)).encode()
    ).hexdigest()
    return f"claude:{h}"


class ClaudeCodeCollector(JsonlTailCollector):
    name = "claude_code"

    def parse_line(self, line: str, path: Path) -> Iterable[TokenEvent]:
        obj = try_json(line)
        if not obj:
            return
        # Only assistant turns carry token usage.
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else None
        usage = (msg or {}).get("usage") or obj.get("usage")
        if not isinstance(usage, dict):
            return
        model = (msg or {}).get("model") or obj.get("model") or "unknown"
        session_id = obj.get("sessionId") or obj.get("session_id")

        input_t  = int(usage.get("input_tokens", 0) or 0)
        output_t = int(usage.get("output_tokens", 0) or 0)
        cache_w  = int(usage.get("cache_creation_input_tokens", 0) or 0)
        cache_r  = int(usage.get("cache_read_input_tokens", 0) or 0)

        if input_t == output_t == cache_w == cache_r == 0:
            return

        ev = TokenEvent(
            source=self.name,
            model=str(model),
            input_tokens=input_t,
            output_tokens=output_t,
            cache_read_tokens=cache_r,
            cache_write_tokens=cache_w,
            cost_usd=cost_for(str(model), input_t, output_t, cache_r, cache_w),
            session_id=session_id,
            timestamp=_ts_of(obj),
            event_id=_stable_id(obj, path),
        )
        yield ev
