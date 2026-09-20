"""A-10 acceptance: the CLI actually participates in the Vault write lock.

``test_locks_cross_process.py`` proves the primitive. This file proves the wiring:
an ``index update`` and a ``note update`` launched as separate processes must not
be able to interleave, because both take the same lock before touching anything.

The assertions are deliberately tolerant of *whether* the two runs overlap — that
is the scheduler's business — and strict about what may happen when they do: a
loser reports the busy error and changes nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from obsai.application.locks import lock_path, vault_lock

REPO = Path(__file__).resolve().parents[2]
OBSAI = Path(sys.executable).with_name("obsai")
BUSY = "already writing to this Vault"

NOTES = {
    "A.md": "# Alpha\n\nAlpha body.\n",
    "B.md": "# Beta\n\nBeta body.\n",
    "C.md": "# Gamma\n\nGamma body.\n",
}

pytestmark = pytest.mark.skipif(not OBSAI.exists(), reason="console script not installed")


def prepare(tmp_path: Path) -> tuple[Path, Path]:
    """A private Vault plus a private config the child processes will read."""
    home = tmp_path / "home"
    vault = home / "vault"
    vault.mkdir(parents=True)
    for name, content in NOTES.items():
        (vault / name).write_text(content, encoding="utf-8")
    database = home / "index.db"
    config = home / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f'[vault]\npath = "{vault}"\n[index]\ndatabase = "{database}"\n', encoding="utf-8"
    )
    return vault, database


def environment(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "home" / "config")
    env["COLUMNS"] = "80"
    env.pop("OPENAI_API_KEY", None)
    for name in [key for key in env if key.startswith("OBSAI_")]:
        env.pop(name)
    env["OBSAI_CONFIG_PATH"] = str(tmp_path / "home" / "config" / "obsai" / "config.toml")
    return env


def run(tmp_path: Path, argv: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(OBSAI), *argv],
        cwd=tmp_path / "home",
        env=environment(tmp_path),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
    )


def flatten(text: str) -> str:
    """Rich wraps the error at COLUMNS; compare on the message, not the layout."""
    return " ".join(text.split())


def squash(text: str) -> str:
    """Every whitespace character removed.

    ``flatten`` is not enough for a path: when Rich folds a token that is longer
    than the remaining width it inserts a space *inside* the path, so
    ``...test_every_writer_waits _for_th0/home/index.db.lock`` no longer contains
    the path as a substring. Whether that happens depends on the length of
    ``TMPDIR``, which made the assertion below silently environment-sensitive.
    Removing all whitespace from both sides makes it layout-independent.
    """
    return "".join(text.split())


def test_every_writer_waits_for_the_lock(tmp_path: Path) -> None:
    """One holder blocks every mutating entry point, and none of them writes.

    The four writers are launched together rather than one after another: they
    contend for the same lock anyway, so this costs one timeout instead of four
    and is closer to what actually happens when a user clicks while a rebuild runs.
    """
    vault, database = prepare(tmp_path)
    assert run(tmp_path, ["index", "update"]).returncode == 0

    cases = [
        (["index", "update"], None),
        (["index", "rebuild"], None),
        (["note", "update", "A.md", "--old", "Alpha body", "--new", "Edited body"], "y\n"),
        (["note", "create", "New.md", "--content", "# New\n"], "y\n"),
    ]
    with vault_lock(database, operation="holder"):
        children = [
            (argv, stdin, subprocess.Popen(
                [str(OBSAI), *argv], cwd=REPO, env=environment(tmp_path),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
            for argv, stdin in cases
        ]
        outcomes = [
            (argv, process.communicate(input=stdin, timeout=120)[1], process.returncode)
            for argv, stdin, process in children
        ]

    for argv, stderr, code in outcomes:
        message = flatten(stderr)
        assert code == 2, f"{argv} was not blocked (exit {code}):\n{stderr}"
        assert BUSY in message, message
        assert " ".join(argv[:2]) in message, message
        assert "Traceback" not in stderr
        assert squash(str(lock_path(database))) in squash(stderr), message

    assert "Alpha body" in (vault / "A.md").read_text(), "a blocked write must change nothing"
    assert not (vault / "New.md").exists()


def test_both_writers_use_one_lock_file(tmp_path: Path) -> None:
    """Same key, or the exclusion above would be an accident of this test's setup."""
    _, database = prepare(tmp_path)
    assert run(tmp_path, ["index", "update"]).returncode == 0
    assert lock_path(database).is_file()
    before = lock_path(database).stat().st_ino
    run(tmp_path, ["note", "update", "A.md", "--old", "Alpha body", "--new", "Edited body"], stdin="y\n")
    assert lock_path(database).stat().st_ino == before


def test_a_read_command_never_takes_the_lock(tmp_path: Path) -> None:
    """Reads must stay concurrent with a long index run."""
    _, database = prepare(tmp_path)
    assert run(tmp_path, ["index", "update"]).returncode == 0
    with vault_lock(database, operation="holder"):
        for argv in (["status"], ["transaction", "status"], ["search", "Alpha", "--mode", "keyword"]):
            result = run(tmp_path, argv)
            assert result.returncode == 0, f"{argv} was blocked:\n{result.stdout}{result.stderr}"
            assert BUSY not in result.stderr


def test_concurrent_index_update_and_note_update_stay_consistent(tmp_path: Path) -> None:
    """Launch both at once and insist the Vault is never left half-written.

    Whether they overlap is up to the scheduler; when they do, the loser has to
    back off cleanly rather than race. Either outcome is acceptable — a partial
    write is not.
    """
    from obsai.storage import Database

    vault, database = prepare(tmp_path)
    assert run(tmp_path, ["index", "update"]).returncode == 0

    index = subprocess.Popen(
        [str(OBSAI), "index", "update"],
        cwd=REPO, env=environment(tmp_path),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    note = subprocess.Popen(
        [str(OBSAI), "note", "update", "A.md", "--old", "Alpha body", "--new", "Edited body"],
        cwd=REPO, env=environment(tmp_path), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    _, note_err = note.communicate(input="y\n", timeout=120)
    _, index_err = index.communicate(timeout=120)

    for name, code, err in (
        ("index", index.returncode, index_err),
        ("note", note.returncode, note_err),
    ):
        assert code in (0, 2), f"{name} exited {code}\n{err}"
        if code == 2:
            assert BUSY in flatten(err), err
            assert "Error:" in err, err

    # The edit either happened completely or not at all.
    content = (vault / "A.md").read_text()
    assert content in ("# Alpha\n\nAlpha body.\n", "# Alpha\n\nEdited body.\n"), content

    # The index is never left structurally broken by a racing update.
    with Database(database) as opened:
        assert opened.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert opened.connection.execute("PRAGMA foreign_key_check").fetchone() is None

    # And whichever way the race went, the Vault is immediately writable again.
    if "Edited body" not in content:
        retry = run(
            tmp_path,
            ["note", "update", "A.md", "--old", "Alpha body", "--new", "Edited body"],
            stdin="y\n",
        )
        assert retry.returncode == 0, retry.stdout + retry.stderr
        assert "Edited body" in (vault / "A.md").read_text()
