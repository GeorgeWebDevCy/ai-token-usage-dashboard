"""Codex / OpenAI CLI session collector.

OpenAI's Codex CLI writes session JSONL files. The exact schema has changed
across releases — sometimes it's rich ``ResponseItem`` records, sometimes just
raw Chat Completions. We look for token usage in whichever envelope shows up.

Shapes we handle:

    {"type":"response_item","response":{"model":"gpt-5",
        "usage":{"input_tokens":12,"output_tokens":34}}}

    {"type":"message","role":"assistant","model":"gpt-5",
        "usage":{"prompt_tokens":12,"completion_tokens":34}}
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Iterable

from ..events import TokenEvent
from ..pricing import cost_for
from .base import JsonlTailCollector, try_json


def _ts_of(obj: dict) -> float:
    for key in ("timestamp", "created_at", "created", "time"):
        raw = obj.get(key)
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
    import time
    return time.time()


def _find_usage(obj: dict) -> tuple[dict | None, str | None]:
    """Walk nested ``response``/``message`` wrappers to find usage + model."""
    candidates = [obj]
    seen_keys = ("response", "message", "data", "item")
    for key in seen_keys:
        v = obj.get(key)
        if isinstance(v, dict):
            candidates.append(v)
    for c in candidates:
        usage = c.get("usage")
        if isinstance(usage, dict):
            model = c.get("model") or obj.get("model")
            return usage, model
    return None, None


def _stable_id(obj: dict, path: Path, line: str) -> str:
    for key in ("id", "response_id", "message_id"):
        if obj.get(key):
            return f"codex:{obj[key]}"
    inner = obj.get("response") or obj.get("message") or {}
    if isinstance(inner, dict):
        for key in ("id", "response_id"):
            if inner.get(key):
                return f"codex:{inner[key]}"
    h = hashlib.sha1((str(path) + "|" + line).encode()).hexdigest()
    return f"codex:{h}"


class CodexCollector(JsonlTailCollector):
    name = "codex"

    def parse_line(self, line: str, path: Path) -> Iterable[TokenEvent]:
        obj = try_json(line)
        if not obj:
            return
        usage, model = _find_usage(obj)
        if not usage:
            return

        # OpenAI uses both prompt_tokens/completion_tokens and (newer) input/output.
        input_t  = int(usage.get("input_tokens",  usage.get("prompt_tokens", 0)) or 0)
        output_t = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
        if input_t == 0 and output_t == 0:
            return

        model = str(model or "unknown")
        ev = TokenEvent(
            source=self.name,
            model=model,
            input_tokens=input_t,
            output_tokens=output_t,
            cost_usd=cost_for(model, input_t, output_t),
            session_id=obj.get("session_id") or obj.get("conversation_id"),
            timestamp=_ts_of(obj),
            event_id=_stable_id(obj, path, line),
        )
        yield ev
