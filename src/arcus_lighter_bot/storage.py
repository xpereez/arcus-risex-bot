from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import Lock

from .models import Cycle, utc_now


class Storage:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT NOT NULL,
              level TEXT NOT NULL,
              kind TEXT NOT NULL,
              message TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cycles (
              id TEXT PRIMARY KEY,
              started_at TEXT NOT NULL,
              ended_at TEXT,
              status TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS metadata (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            """
        )
        self._db.commit()

    def event(self, kind: str, message: str, payload: dict | None = None, level: str = "info") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO events(created_at, level, kind, message, payload) VALUES (?, ?, ?, ?, ?)",
                (utc_now().isoformat(), level, kind, message, json.dumps(payload or {})),
            )
            self._db.commit()

    def save_cycle(self, cycle: Cycle) -> None:
        payload = cycle.to_dict()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO cycles(id, started_at, ended_at, status, payload)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  ended_at=excluded.ended_at,
                  status=excluded.status,
                  payload=excluded.payload
                """,
                (
                    cycle.id,
                    payload["started_at"],
                    payload["ended_at"],
                    cycle.status,
                    json.dumps(payload),
                ),
            )
            self._db.commit()

    def recent_events(self, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT created_at, level, kind, message, payload FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "created_at": row["created_at"],
                "level": row["level"],
                "kind": row["kind"],
                "message": row["message"],
                "payload": json.loads(row["payload"]),
            }
            for row in rows
        ]

    def recent_cycles(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM cycles ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def metadata(self, key: str) -> object | None:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM metadata WHERE key = ?", (key,)
            ).fetchone()
        return json.loads(row["value"]) if row else None

    def set_metadata_if_absent(self, key: str, value: object) -> object:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )
            row = self._db.execute(
                "SELECT value FROM metadata WHERE key = ?", (key,)
            ).fetchone()
            self._db.commit()
        return json.loads(row["value"])
