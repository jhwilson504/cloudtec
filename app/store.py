"""SQLite-backed storage for saved connections.

Non-secret settings (host, base DN, ...) are stored as JSON. The password for
each connection is encrypted with Fernet before it touches disk, and is never
returned by the API.
"""

import json
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

KINDS = ("ad", "vsphere")


@dataclass
class Connection:
    id: str
    kind: str
    name: str
    config: dict
    secret: str
    demo: bool
    created_at: str

    def public(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "config": self.config,
            "demo": self.demo,
            "created_at": self.created_at,
        }


def _write_private(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)


def load_or_create_key(data_dir: Path, configured: str | None) -> bytes:
    if configured:
        return configured.encode()
    path = data_dir / "secret.key"
    if not path.exists():
        _write_private(path, Fernet.generate_key().decode())
    return path.read_text().strip().encode()


class Store:
    def __init__(self, data_dir: Path, key: bytes):
        data_dir.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(key)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(data_dir / "cloudtec.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS connections (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                config TEXT NOT NULL,
                secret BLOB NOT NULL,
                demo INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )"""
        )
        self._db.commit()

    def _row(self, row: sqlite3.Row) -> Connection:
        return Connection(
            id=row["id"],
            kind=row["kind"],
            name=row["name"],
            config=json.loads(row["config"]),
            secret=self._fernet.decrypt(row["secret"]).decode(),
            demo=bool(row["demo"]),
            created_at=row["created_at"],
        )

    def list(self, kind: str | None = None) -> list[Connection]:
        with self._lock:
            if kind:
                rows = self._db.execute(
                    "SELECT * FROM connections WHERE kind = ? ORDER BY name", (kind,)
                ).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM connections ORDER BY kind, name").fetchall()
        return [self._row(r) for r in rows]

    def get(self, conn_id: str) -> Connection | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM connections WHERE id = ?", (conn_id,)).fetchone()
        return self._row(row) if row else None

    def add(self, kind: str, name: str, config: dict, secret: str, demo: bool) -> Connection:
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        conn = Connection(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            name=name,
            config=config,
            secret=secret,
            demo=demo,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        with self._lock:
            self._db.execute(
                "INSERT INTO connections VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    conn.id,
                    conn.kind,
                    conn.name,
                    json.dumps(conn.config),
                    self._fernet.encrypt(secret.encode()),
                    int(demo),
                    conn.created_at,
                ),
            )
            self._db.commit()
        return conn

    def delete(self, conn_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM connections WHERE id = ?", (conn_id,))
            self._db.commit()
        return cur.rowcount > 0
