"""SQLite persistence."""

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
    session_id          TEXT,
    project             TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);
"""

_MIGRATIONS = [
    "ALTER TABLE events ADD COLUMN project TEXT",
    "CREATE INDEX IF NOT EXISTS idx_events_project ON events(project)",
]


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    def _migrate(self, c: sqlite3.Connection) -> None:
        for sql in _MIGRATIONS:
            try:
                c.execute(sql)
            except sqlite3.OperationalError:
                pass  # column already exists

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
        with self._conn() as c:
            try:
                c.execute(
                    """
                    INSERT INTO events (
                        event_id, timestamp, source, model,
                        input_tokens, output_tokens,
                        cache_read_tokens, cache_write_tokens,
                        cost_usd, session_id, project
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        e.event_id, e.timestamp, e.source, e.model,
                        e.input_tokens, e.output_tokens,
                        e.cache_read_tokens, e.cache_write_tokens,
                        e.cost_usd, e.session_id, e.project,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def backfill_projects(self, session_project_map: dict[str, str]) -> int:
        """Update project for existing events that have a NULL project but known session_id."""
        if not session_project_map:
            return 0
        updated = 0
        with self._conn() as c:
            for session_id, project in session_project_map.items():
                cur = c.execute(
                    "UPDATE events SET project = ? WHERE session_id = ? AND project IS NULL",
                    (project, session_id),
                )
                updated += cur.rowcount
        return updated

    def row_count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def earliest_ts(self) -> float | None:
        with self._conn() as c:
            row = c.execute("SELECT MIN(timestamp) FROM events").fetchone()
            return row[0] if row and row[0] is not None else None

    def _ts_clause(self, since_ts: float, until_ts: float | None) -> tuple[str, tuple]:
        if until_ts is not None:
            return "timestamp >= ? AND timestamp <= ?", (since_ts, until_ts)
        return "timestamp >= ?", (since_ts,)

    def totals_since(self, since_ts: float, until_ts: float | None = None) -> dict:
        clause, params = self._ts_clause(since_ts, until_ts)
        with self._conn() as c:
            row = c.execute(
                f"""
                SELECT
                    COALESCE(SUM(input_tokens), 0)       AS input_tokens,
                    COALESCE(SUM(output_tokens), 0)      AS output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0)  AS cache_read_tokens,
                    COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                    COALESCE(SUM(cost_usd), 0.0)         AS cost_usd,
                    COUNT(*)                             AS event_count
                FROM events WHERE {clause}
                """,
                params,
            ).fetchone()
            return dict(row)

    def by_source_since(self, since_ts: float, until_ts: float | None = None) -> list[dict]:
        clause, params = self._ts_clause(since_ts, until_ts)
        with self._conn() as c:
            rows = c.execute(
                f"""
                SELECT source,
                       SUM(input_tokens + output_tokens
                           + cache_read_tokens + cache_write_tokens) AS tokens,
                       SUM(cost_usd) AS cost_usd,
                       COUNT(*) AS event_count
                FROM events WHERE {clause}
                GROUP BY source
                ORDER BY tokens DESC
                """,
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def by_model_since(self, since_ts: float, until_ts: float | None = None) -> list[dict]:
        clause, params = self._ts_clause(since_ts, until_ts)
        with self._conn() as c:
            rows = c.execute(
                f"""
                SELECT source, model,
                       SUM(input_tokens)       AS input_tokens,
                       SUM(output_tokens)      AS output_tokens,
                       SUM(cache_read_tokens)  AS cache_read_tokens,
                       SUM(cache_write_tokens) AS cache_write_tokens,
                       SUM(cost_usd)           AS cost_usd,
                       COUNT(*)                AS event_count
                FROM events WHERE {clause}
                GROUP BY source, model
                ORDER BY (SUM(input_tokens) + SUM(output_tokens)) DESC
                """,
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def cache_savings_by_model(self, since_ts: float, until_ts: float | None = None) -> list[dict]:
        """Per-model cache_read totals for savings computation."""
        clause, params = self._ts_clause(since_ts, until_ts)
        with self._conn() as c:
            rows = c.execute(
                f"""
                SELECT model,
                       SUM(cache_read_tokens) AS cache_read_tokens,
                       SUM(cache_write_tokens) AS cache_write_tokens,
                       SUM(input_tokens) AS input_tokens
                FROM events WHERE {clause} AND cache_read_tokens > 0
                GROUP BY model
                """,
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def by_project_since(self, since_ts: float, until_ts: float | None = None) -> list[dict]:
        clause, params = self._ts_clause(since_ts, until_ts)
        with self._conn() as c:
            rows = c.execute(
                f"""
                SELECT COALESCE(project, 'unknown') AS project,
                       source,
                       SUM(input_tokens + output_tokens
                           + cache_read_tokens + cache_write_tokens) AS tokens,
                       SUM(cost_usd) AS cost_usd,
                       COUNT(*) AS event_count
                FROM events WHERE {clause}
                GROUP BY project, source
                ORDER BY tokens DESC
                """,
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def buckets_since(self, since_ts: float, bucket_seconds: int, until_ts: float | None = None) -> list[dict]:
        clause, ts_params = self._ts_clause(since_ts, until_ts)
        with self._conn() as c:
            rows = c.execute(
                f"""
                SELECT
                    CAST(timestamp / ? AS INTEGER) * ? AS bucket_ts,
                    source,
                    SUM(input_tokens + output_tokens
                        + cache_read_tokens + cache_write_tokens) AS tokens
                FROM events WHERE {clause}
                GROUP BY bucket_ts, source
                ORDER BY bucket_ts ASC
                """,
                (bucket_seconds, bucket_seconds) + ts_params,
            ).fetchall()
            return [dict(r) for r in rows]

    def recent(self, limit: int = 100, since_ts: float | None = None, until_ts: float | None = None) -> list[dict]:
        with self._conn() as c:
            if since_ts is not None:
                clause, params = self._ts_clause(since_ts, until_ts)
                rows = c.execute(
                    f"SELECT * FROM events WHERE {clause} ORDER BY timestamp DESC LIMIT ?",
                    params + (limit,),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM events ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
