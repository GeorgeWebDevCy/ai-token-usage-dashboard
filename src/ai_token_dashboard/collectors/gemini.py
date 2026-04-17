"""Gemini CLI session collector.

Gemini CLI persists session logs as JSONL. Token accounting is under a
``usageMetadata`` object (matching the Gemini REST API), with
``promptTokenCount`` / ``candidatesTokenCount``. Cached tokens (when reported)
live under ``cachedContentTokenCount``.
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
    for key in ("timestamp", "createTime", "time"):
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
    """Gemini nests usage deep; walk common containers."""
    candidates = [obj]
    for key in ("response", "data", "result", "message"):
        v = obj.get(key)
        if isinstance(v, dict):
            candidates.append(v)
    for c in candidates:
        for key in ("usageMetadata", "usage_metadata", "usage"):
            u = c.get(key)
            if isinstance(u, dict):
                model = c.get("model") or c.get("modelVersion") or obj.get("model")
                return u, model
    return None, None


class GeminiCollector(JsonlTailCollector):
    name = "gemini"

    def parse_line(self, line: str, path: Path) -> Iterable[TokenEvent]:
        obj = try_json(line)
        if not obj:
            return
        usage, model = _find_usage(obj)
        if not usage:
            return

        input_t  = int(usage.get("promptTokenCount",      usage.get("prompt_tokens",      0)) or 0)
        output_t = int(usage.get("candidatesTokenCount",  usage.get("completion_tokens",  0)) or 0)
        cache_r  = int(usage.get("cachedContentTokenCount", 0) or 0)
        if input_t == output_t == cache_r == 0:
            return

        model = str(model or "gemini-unknown")
        event_id = obj.get("id") or hashlib.sha1((str(path) + "|" + line).encode()).hexdigest()
        ev = TokenEvent(
            source=self.name,
            model=model,
            input_tokens=input_t,
            output_tokens=output_t,
            cache_read_tokens=cache_r,
            cost_usd=cost_for(model, input_t, output_t, cache_read_tokens=cache_r),
            session_id=obj.get("sessionId") or obj.get("session_id"),
            timestamp=_ts_of(obj),
            event_id=f"gemini:{event_id}",
        )
        yield ev
