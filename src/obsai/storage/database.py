"""SQLite connection policy and explicit, nestable transactions."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from obsai.errors import SchemaError
from obsai.storage.schema import initialize_schema


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path) if path != ":memory:" else path
        self.connection = sqlite3.connect(path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        if self.connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            self.connection.close()
            raise SchemaError("SQLite foreign key enforcement is unavailable")
        try:
            initialize_schema(self.connection)
        except Exception:
            self.connection.close()
            raise
        self._savepoint_number = 0

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A nested call uses a savepoint, so outer rollback still wins."""
        nested = self.connection.in_transaction
        if nested:
            self._savepoint_number += 1
            name = f"obsai_sp_{self._savepoint_number}"
            self.connection.execute(f"SAVEPOINT {name}")
        else:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except BaseException:
            if nested:
                self.connection.execute(f"ROLLBACK TO {name}")
                self.connection.execute(f"RELEASE {name}")
            else:
                self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute(f"RELEASE {name}" if nested else "COMMIT")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
