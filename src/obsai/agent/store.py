"""Separate, durable tool artifacts. Checkpoints contain opaque refs only."""

import json
import sqlite3
from pathlib import Path
from uuid import uuid4


class ArtifactStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self.connection.commit()

    def put(self, value: object) -> str:
        ref = uuid4().hex
        self.connection.execute(
            "INSERT INTO artifacts VALUES (?, ?)",
            (ref, json.dumps(value, ensure_ascii=False, sort_keys=True)),
        )
        self.connection.commit()
        return ref

    def get(self, ref: str) -> object:
        row = self.connection.execute(
            "SELECT payload FROM artifacts WHERE id = ?", (ref,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Missing agent artifact: {ref}")
        return json.loads(row[0])

    def close(self) -> None:
        self.connection.close()
