"""Billing / usage API pollers.

These pull authoritative numbers from the vendors' usage endpoints. They're
best used as a *reconciliation* source: tally up any usage that didn't come
through the local CLIs (web app, other machines, desktop app). Each vendor's
endpoint is slightly different and requires an admin-scoped key.

Design notes:

* Each poller tracks the last timestamp it ingested in a tiny state file under
  the app dir, so restarting the daemon doesn't re-ingest the same window.
* If a required key is missing or the endpoint responds with an error, we log
  and sleep; we never crash the whole process.
* These are best-effort: if a vendor changes their endpoint schema we fall
  back to zeros rather than corrupting the DB.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import AsyncIterator

import httpx

from ..events import TokenEvent
from ..pricing import cost_for
from .base import PollingCollector

log = logging.getLogger(__name__)


class _StatefulPoller(PollingCollector):
    state_file_name: str

    def __init__(self, db, app_dir: Path, api_key: str | None, interval_s: int) -> None:
        super().__init__(db)
        self.api_key = api_key
        self.interval_s = interval_s
        self._state_path = app_dir / self.state_file_name
        self._last_ts: float = self._load_state()

    def _load_state(self) -> float:
        if not self._state_path.exists():
            return time.time() - 24 * 3600  # ingest the past day on first run
        try:
            return float(json.loads(self._state_path.read_text()).get("last_ts", 0))
        except Exception:  # noqa: BLE001
            return time.time() - 24 * 3600

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps({"last_ts": self._last_ts}))


class AnthropicApiCollector(_StatefulPoller):
    """Polls Anthropic's Admin API usage endpoint."""
    name = "anthropic_api"
    state_file_name = "anthropic_state.json"

    async def poll(self) -> AsyncIterator[TokenEvent]:
        if not self.api_key:
            return
        # https://docs.claude.com — usage/cost admin endpoint
        url = "https://api.anthropic.com/v1/organizations/usage_report/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        params = {"starting_at": int(self._last_ts), "limit": 100}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(url, headers=headers, params=params)
                r.raise_for_status()
                data = r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("anthropic poll: %s", e)
            return
        max_ts = self._last_ts
        for bucket in data.get("data", []):
            for item in bucket.get("results", []):
                model = item.get("model", "unknown")
                input_t = int(item.get("uncached_input_tokens", 0) or 0)
                output_t = int(item.get("output_tokens", 0) or 0)
                cache_r = int(item.get("cache_read_input_tokens", 0) or 0)
                cache_w = int(item.get("cache_creation_input_tokens", 0) or 0)
                ts = float(bucket.get("ending_at") or time.time())
                max_ts = max(max_ts, ts)
                yield TokenEvent(
                    source=self.name,
                    model=model,
                    input_tokens=input_t,
                    output_tokens=output_t,
                    cache_read_tokens=cache_r,
                    cache_write_tokens=cache_w,
                    cost_usd=cost_for(model, input_t, output_t, cache_r, cache_w),
                    timestamp=ts,
                    event_id=f"anthropic_api:{ts}:{model}",
                )
        self._last_ts = max_ts
        self._save_state()


class OpenAIApiCollector(_StatefulPoller):
    """Polls OpenAI's organization usage endpoint."""
    name = "openai_api"
    state_file_name = "openai_state.json"

    async def poll(self) -> AsyncIterator[TokenEvent]:
        if not self.api_key:
            return
        url = "https://api.openai.com/v1/organization/usage/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        params = {"start_time": int(self._last_ts), "bucket_width": "1h", "limit": 100}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(url, headers=headers, params=params)
                r.raise_for_status()
                data = r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("openai poll: %s", e)
            return
        max_ts = self._last_ts
        for bucket in data.get("data", []):
            ts = float(bucket.get("end_time") or bucket.get("start_time") or time.time())
            for item in bucket.get("results", []):
                model = item.get("model", "unknown")
                input_t = int(item.get("input_tokens", 0) or 0)
                output_t = int(item.get("output_tokens", 0) or 0)
                max_ts = max(max_ts, ts)
                yield TokenEvent(
                    source=self.name,
                    model=model,
                    input_tokens=input_t,
                    output_tokens=output_t,
                    cost_usd=cost_for(model, input_t, output_t),
                    timestamp=ts,
                    event_id=f"openai_api:{ts}:{model}",
                )
        self._last_ts = max_ts
        self._save_state()


class GoogleApiCollector(_StatefulPoller):
    """Google doesn't expose a per-user usage endpoint; this is a placeholder.

    When Google ships a usage API that matches, flip ``enabled`` and wire the
    HTTP call here. For now we rely on the Gemini CLI file-watcher for local
    usage.
    """
    name = "google_api"
    state_file_name = "google_state.json"

    async def poll(self) -> AsyncIterator[TokenEvent]:
        if False:
            yield  # type: ignore[unreachable]
        return
