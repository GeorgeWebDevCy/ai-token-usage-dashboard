"""SQLite persistence.

One table, ``events``, one row per assistant turn. SQLite is plenty here — a
year of heavy use is perhaps a few hundred thousand rows and the dashboard only
queries recent windows.

We open a fresh connection per operation (``sqlite3.connect`` is cheap) so we
don't have to worry about cross-thread reuse: collectors run in background
threads, FastAPI handlers run in the event loop, and pystray runs on the main
thread.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .events import TokenEvent


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id            TEXT PRIMARY KEY,
    timestamp           REAL NOT NULL,
    source              TEXT NOT NULL,
    model               TEXT NOT NULL,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd            REAL NOT NULL DEFAULT 0.0,
    session_id          TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def insert(self, e: TokenEvent) -> bool:
        """Insert an event. Returns False if the event_id already existed.

        Dedupe matters because file-watch collectors may re-read lines after a
        truncation/rotation; a stable ``event_id`` from the source makes the
        insert idempotent.
        """
        with self._conn() as c:
            try:
                c.execute(
                    """
                    INSERT INTO events (
                        event_id, timestamp, source, model,
                        input_tokens, output_tokens,
                        cache_read_tokens, cache_write_tokens,
                        cost_usd, session_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        e.event_id, e.timestamp, e.source, e.model,
                        e.input_tokens, e.output_tokens,
                        e.cache_read_tokens, e.cache_write_tokens,
                        e.cost_usd, e.session_id,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    # -------- queries used by the API --------

    def totals_since(self, since_ts: float) -> dict:
        """Aggregate totals for events newer than ``since_ts``."""
        with self._conn() as c:
            row = c.execute(
                """
                SELECT
                    COALESCE(SUM(input_tokens), 0)       AS input_tokens,
                    COALESCE(SUM(output_tokens), 0)      AS output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0)  AS cache_read_tokens,
                    COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                    COALESCE(SUM(cost_usd), 0.0)         AS cost_usd,
                    COUNT(*)                             AS event_count
                FROM events WHERE timestamp >= ?
                """,
                (since_ts,),
            ).fetchone()
            return dict(row)

    def by_source_since(self, since_ts: float) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT source,
                       SUM(input_tokens + output_tokens
                           + cache_read_tokens + cache_write_tokens) AS tokens,
                       SUM(cost_usd) AS cost_usd,
                       COUNT(*) AS event_count
                FROM events WHERE timestamp >= ?
                GROUP BY source
                ORDER BY tokens DESC
                """,
                (since_ts,),
            ).fetchall()
            return [dict(r) for r in rows]

    def by_model_since(self, since_ts: float) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT source, model,
                       SUM(input_tokens)       AS input_tokens,
                       SUM(output_tokens)      AS output_tokens,
                       SUM(cache_read_tokens)  AS cache_read_tokens,
                       SUM(cache_write_tokens) AS cache_write_tokens,
                       SUM(cost_usd)           AS cost_usd,
                       COUNT(*)                AS event_count
                FROM events WHERE timestamp >= ?
                GROUP BY source, model
                ORDER BY (SUM(input_tokens) + SUM(output_tokens)) DESC
                """,
                (since_ts,),
            ).fetchall()
            return [dict(r) for r in rows]

    def buckets_since(self, since_ts: float, bucket_seconds: int) -> list[dict]:
        """Time-bucketed series for the dashboard's area chart."""
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT
                    CAST(timestamp / ? AS INTEGER) * ? AS bucket_ts,
                    source,
                    SUM(input_tokens + output_tokens
                        + cache_read_tokens + cache_write_tokens) AS tokens
                FROM events WHERE timestamp >= ?
                GROUP BY bucket_ts, source
                ORDER BY bucket_ts ASC
                """,
                (bucket_seconds, bucket_seconds, since_ts),
            ).fetchall()
            return [dict(r) for r in rows]

    def recent(self, limit: int = 100) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM events ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
