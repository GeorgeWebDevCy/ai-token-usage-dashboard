"""Collector base classes.

Two flavors:

* ``JsonlTailCollector`` — watches one or more directories for ``*.jsonl``
  files, tails them, parses each new line. Subclasses implement ``parse_line``.
* ``PollingCollector`` — runs ``poll()`` on an interval in the event loop.
  Subclasses implement ``poll``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from ..db import Database
from ..events import BUS, TokenEvent

log = logging.getLogger(__name__)


class _TailState:
    """Tracks byte offset per file so we don't re-read what we've already seen."""

    def __init__(self) -> None:
        self._offsets: dict[Path, int] = {}
        self._lock = threading.Lock()

    def read_new(self, path: Path) -> list[str]:
        """Return any lines appended to ``path`` since the last call."""
        lines: list[str] = []
        with self._lock:
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                self._offsets.pop(path, None)
                return lines
            start = self._offsets.get(path, 0)
            # File was truncated / rotated; start from top.
            if size < start:
                start = 0
            if size == start:
                return lines
            try:
                with path.open("rb") as f:
                    f.seek(start)
                    data = f.read(size - start)
                self._offsets[path] = size
            except OSError as e:
                log.warning("tail: can't read %s: %s", path, e)
                return lines
            for raw in data.splitlines():
                s = raw.decode("utf-8", errors="replace").strip()
                if s:
                    lines.append(s)
        return lines

    def seed(self, path: Path) -> None:
        """Mark an existing file as 'already consumed' so we only capture new activity.

        First run: we don't want to dump months of old tokens into the DB
        (and they're probably already there from a previous run anyway, since
        inserts dedupe on event_id).
        """
        try:
            self._offsets[path] = path.stat().st_size
        except FileNotFoundError:
            pass


class JsonlTailCollector(ABC, FileSystemEventHandler):
    """File-watcher collector for JSONL session logs.

    Subclasses:
      - set ``name`` (e.g. "claude_code")
      - implement ``parse_line(line, path) -> Iterable[TokenEvent]``
    """

    name: str = ""

    def __init__(
        self,
        paths: Iterable[Path],
        db: Database,
        loop: asyncio.AbstractEventLoop,
        ingest_existing: bool = False,
    ) -> None:
        self.paths = [p for p in paths if p is not None]
        self.db = db
        self.loop = loop
        self.ingest_existing = ingest_existing
        self._state = _TailState()
        self._observer: Observer | None = None

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._observer = Observer()
        for root in self.paths:
            if not root.exists():
                log.info("%s: path %s doesn't exist yet; watching parent", self.name, root)
                # Watch the parent so the dir appearing later still triggers us.
                parent = root.parent
                if parent.exists():
                    self._observer.schedule(self, str(parent), recursive=True)
                continue
            self._observer.schedule(self, str(root), recursive=True)
            # Scan existing files — seed or ingest depending on mode.
            for p in root.rglob("*.jsonl"):
                if self.ingest_existing:
                    self._drain(p)
                else:
                    self._state.seed(p)
        self._observer.start()
        log.info("%s collector watching %s", self.name, [str(p) for p in self.paths])

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=2.0)

    # --- watchdog hooks --------------------------------------------------

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        p = Path(event.src_path)
        if p.suffix == ".jsonl":
            self._drain(p)

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        p = Path(event.src_path)
        if p.suffix == ".jsonl":
            self._drain(p)

    # --- parsing ---------------------------------------------------------

    def _drain(self, path: Path) -> None:
        for line in self._state.read_new(path):
            try:
                for ev in self.parse_line(line, path):
                    self._emit(ev)
            except Exception as e:  # noqa: BLE001 - per-line robustness
                log.debug("%s: parse error in %s: %s", self.name, path, e)

    def _emit(self, ev: TokenEvent) -> None:
        if not self.db.insert(ev):
            return
        # Publish to the asyncio bus from this watchdog thread. In one-shot
        # scan mode the loop may not be running and we have no subscribers, so
        # skip the publish and avoid a 'coroutine never awaited' warning.
        loop = self.loop
        if loop is None or not loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(BUS.publish(ev), loop)

    @abstractmethod
    def parse_line(self, line: str, path: Path) -> Iterable[TokenEvent]:
        """Parse one JSONL line. Yield zero or more events."""
        ...


class PollingCollector(ABC):
    """Periodic poller running in the asyncio loop."""

    name: str = ""
    interval_s: int = 300

    def __init__(self, db: Database) -> None:
        self.db = db
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._task = loop.create_task(self._run())

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                async for ev in self.poll():
                    if self.db.insert(ev):
                        await BUS.publish(ev)
            except Exception as e:  # noqa: BLE001
                log.warning("%s poll failed: %s", self.name, e)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            await self._task

    @abstractmethod
    async def poll(self):
        """Yield TokenEvents for whatever new usage has shown up."""
        ...


def try_json(line: str) -> dict | None:
    try:
        obj = json.loads(line)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None
