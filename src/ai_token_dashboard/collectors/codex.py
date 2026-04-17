"""Codex CLI session collector.

Codex writes JSONL files at ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl

Relevant event types (all have top-level "timestamp" and "type"):

  {"type":"session_meta","payload":{"id":"<uuid>","cwd":"<path>","model_provider":"openai",...}}
  {"type":"turn_context","payload":{"turn_id":"<uuid>","model":"gpt-5.4","cwd":"<path>",...}}
  {"type":"event_msg","payload":{"type":"task_started","turn_id":"<uuid>",...}}
  {"type":"event_msg","payload":{"type":"token_count","info":{
      "last_token_usage":{
          "input_tokens":N,"cached_input_tokens":N,
          "output_tokens":N,"reasoning_output_tokens":N
      },...}}}

token_count fires multiple times per turn (once per API call). last_token_usage
is the incremental delta since the previous event. We emit one TokenEvent per
token_count so costs sum correctly; the stable event_id deduplicates re-reads.
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
    raw = obj.get("timestamp")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    import time
    return time.time()


class CodexCollector(JsonlTailCollector):
    name = "codex"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Per-file state: maps path → {session_id, model, cwd, turn_id}
        self._file_state: dict[Path, dict] = {}

    def _state_for(self, path: Path) -> dict:
        if path not in self._file_state:
            self._file_state[path] = {
                "session_id": path.stem,
                "model": "gpt-5",
                "cwd": None,
                "turn_id": None,
            }
        return self._file_state[path]

    def _line_id(self, line: str, path: Path) -> str:
        return "codex:" + hashlib.sha1((str(path) + "|" + line[:200]).encode()).hexdigest()

    def parse_line(self, line: str, path: Path) -> Iterable[TokenEvent]:
        obj = try_json(line)
        if not obj:
            return

        st = self._state_for(path)
        kind = obj.get("type")

        if kind == "session_meta":
            p = obj.get("payload") or {}
            if p.get("id"):
                st["session_id"] = p["id"]
            if p.get("cwd"):
                st["cwd"] = p["cwd"]
            return

        if kind == "turn_context":
            p = obj.get("payload") or {}
            if p.get("model"):
                st["model"] = p["model"]
            if p.get("cwd"):
                st["cwd"] = p["cwd"]
            if p.get("turn_id"):
                st["turn_id"] = p["turn_id"]
            return

        if kind == "event_msg":
            p = obj.get("payload") or {}
            if p.get("type") == "task_started" and p.get("turn_id"):
                st["turn_id"] = p["turn_id"]
                return

            if p.get("type") == "token_count":
                info = p.get("info") or {}
                usage = info.get("last_token_usage") or {}
                input_t  = int(usage.get("input_tokens", 0) or 0)
                output_t = int(usage.get("output_tokens", 0) or 0)
                cache_r  = int(usage.get("cached_input_tokens", 0) or 0)
                # reasoning tokens are already counted in output_tokens
                if input_t == output_t == cache_r == 0:
                    return

                event_id = self._line_id(line, path)

                # Project: last path component of cwd, or slug the full path
                cwd = st.get("cwd")
                if cwd:
                    project = Path(cwd).name or cwd
                else:
                    # Fall back to parent dir of the JSONL (YYYY/MM/DD → not useful)
                    project = None

                model = st["model"]
                yield TokenEvent(
                    source=self.name,
                    model=model,
                    input_tokens=input_t,
                    output_tokens=output_t,
                    cache_read_tokens=cache_r,
                    cost_usd=cost_for(model, input_t, output_t, cache_r),
                    session_id=st["session_id"],
                    project=project,
                    timestamp=_ts_of(obj),
                    event_id=event_id,
                )
