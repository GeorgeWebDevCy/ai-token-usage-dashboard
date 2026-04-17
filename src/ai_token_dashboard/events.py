"""Token event types and an in-process pub/sub bus.

A ``TokenEvent`` represents one AI assistant turn with its token accounting.
Collectors publish events to the bus; the DB writer and WebSocket broadcaster
both subscribe. Keeping this in-process (no Redis/NATS) is the right call for a
single-user local app.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncIterator


@dataclass(slots=True)
class TokenEvent:
    """One assistant turn's token accounting.

    Attributes are a superset of what each vendor emits so the same row shape
    fits Claude (which has cache tokens), OpenAI (which doesn't), and Gemini.
    Missing fields default to 0.
    """

    source: str                 # "claude_code", "codex", "gemini", "anthropic_api", ...
    model: str                  # e.g. "claude-sonnet-4-6"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    session_id: str | None = None
    project: str | None = None  # folder slug, e.g. "D--GitHub-Projects-myapp"
    # Unix seconds; using float so sub-second ordering is preserved.
    timestamp: float = field(default_factory=lambda: time.time())
    # Stable per-event id so we can dedupe on re-reads of append-only JSONL.
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventBus:
    """Async fan-out bus. Subscribers each get their own queue."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[TokenEvent]] = []
        self._lock = asyncio.Lock()

    async def publish(self, event: TokenEvent) -> None:
        async with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            # Never block the publisher on a slow subscriber — drop if full.
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def subscribe(self) -> AsyncIterator[TokenEvent]:
        q: asyncio.Queue[TokenEvent] = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._subscribers.append(q)
        try:
            while True:
                yield await q.get()
        finally:
            async with self._lock:
                if q in self._subscribers:
                    self._subscribers.remove(q)


# A process-wide singleton so collectors and the server share one bus.
BUS = EventBus()
