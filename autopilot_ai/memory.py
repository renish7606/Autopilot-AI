"""Local SQLite storage for task sessions and verified tool steps."""

import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any
from uuid import uuid4


DEFAULT_MEMORY_PATH = Path(__file__).resolve().parents[1] / "data" / "autopilot_memory.db"


class TaskMemory:
    """Persist lightweight task history in a local database."""

    def __init__(self, path: Path | str = DEFAULT_MEMORY_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    last_active TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    output_json TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions (session_id)
                );
                CREATE INDEX IF NOT EXISTS task_history_session_id_id
                    ON task_history(session_id, id DESC);
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def create_session(self, session_id: str | None = None) -> str:
        selected = session_id or str(uuid4())
        now = self._now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions(session_id, created_at, last_active) VALUES (?, ?, ?)",
                (selected, now, now),
            )
        return selected

    def has_session(self, session_id: str) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone() is not None

    def record_step(
        self,
        session_id: str,
        goal: str,
        tool_name: str,
        status: str,
        summary: str,
        output: Any,
    ) -> None:
        now = self._now()
        output_json = json.dumps(output, ensure_ascii=False, separators=(",", ":"), default=str)
        with self._connect() as connection:
            connection.execute("UPDATE sessions SET last_active = ? WHERE session_id = ?", (now, session_id))
            connection.execute(
                """INSERT INTO task_history
                   (session_id, timestamp, goal, tool_name, status, summary, output_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session_id, now, goal, tool_name, status, summary, output_json),
            )

    def get_recent_history(self, session_id: str, limit: int = 10) -> list[dict[str, Any]]:
        bounded = _bounded_limit(limit, 100)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM task_history WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, bounded),
            ).fetchall()
        return [_row_to_dict(row) for row in reversed(rows)]

    def get_recent_sessions_history(self, limit: int = 10) -> list[dict[str, Any]]:
        bounded = _bounded_limit(limit, 100)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM task_history ORDER BY id DESC LIMIT ?", (bounded,)
            ).fetchall()
        return [_row_to_dict(row) for row in reversed(rows)]

    def search_memory(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        text = query.strip()
        if not text:
            return []
        bounded = _bounded_limit(limit, 100)
        pattern = f"%{text}%"
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM task_history
                   WHERE goal LIKE ? OR tool_name LIKE ? OR status LIKE ? OR summary LIKE ?
                   ORDER BY id DESC LIMIT ?""",
                (pattern, pattern, pattern, pattern, bounded),
            ).fetchall()
        return [_row_to_dict(row) for row in reversed(rows)]


def _bounded_limit(limit: int, maximum: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError("limit must be an integer")
    return max(1, min(limit, maximum))


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    encoded = item.pop("output_json")
    try:
        item["output"] = json.loads(encoded) if encoded is not None else None
    except (TypeError, json.JSONDecodeError):
        item["output"] = None
    return item
