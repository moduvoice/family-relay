from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class SqliteStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS items (pk TEXT NOT NULL, sk TEXT NOT NULL, doc TEXT NOT NULL, PRIMARY KEY (pk, sk))"
        )
        self._db.commit()

    def get(self, pk: str, sk: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT doc FROM items WHERE pk=? AND sk=?", (pk, sk)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, pk: str, sk: str, doc: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO items (pk, sk, doc) VALUES (?, ?, ?) ON CONFLICT(pk, sk) DO UPDATE SET doc=excluded.doc",
                (pk, sk, json.dumps(doc, ensure_ascii=False)),
            )
            self._db.commit()

    def query(self, pk: str, sk_prefix: str = "") -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT doc FROM items WHERE pk=? AND substr(sk, 1, ?)=? ORDER BY sk",
                (pk, len(sk_prefix), sk_prefix),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def delete(self, pk: str, sk: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM items WHERE pk=? AND sk=?", (pk, sk))
            self._db.commit()

    def delete_partition(self, pk: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM items WHERE pk=?", (pk,))
            self._db.commit()
