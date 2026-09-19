"""Cross-process behaviour of the Vault write lock.

``flock`` is the only part of the locking story that cannot be tested in one
interpreter, so these tests spawn real child processes and read a shared JSONL
log to reconstruct who held the lock when.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from obsai.application.locks import lock_path, vault_lock

REPO = Path(__file__).resolve().parents[2]

_CHILD = textwrap.dedent(
    """
    import json, sys, time
    from pathlib import Path

    from obsai.application.locks import LockBusyError, vault_lock

    database, label, timeout, log_path, hold = (
        Path(sys.argv[1]), sys.argv[2], float(sys.argv[3]), Path(sys.argv[4]), float(sys.argv[5])
    )

    def record(kind):
        with log_path.open("a") as handle:
            handle.write(json.dumps({"label": label, "kind": kind, "at": time.time()}) + "\\n")
            handle.flush()

    try:
        with vault_lock(database, operation=label, timeout=timeout):
            record("enter")
            time.sleep(hold)
            record("exit")
    except LockBusyError:
        record("busy")
        raise SystemExit(3)
    except BaseException as exc:
        record("error")
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(4)
    """
)


def _spawn(database: Path, label: str, *, timeout: float, hold: float, log: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(database), label, str(timeout), str(log), str(hold)],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _events(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def _wait_for(log: Path, label: str, kind: str, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(e["label"] == label and e["kind"] == kind for e in _events(log)):
            return
        time.sleep(0.02)
    raise AssertionError(f"{label} never recorded {kind}: {_events(log)}")


@pytest.fixture()
def database(tmp_path: Path) -> Path:
    return tmp_path / "state" / "index.db"


def test_two_processes_never_hold_the_lock_at_once(database: Path, tmp_path: Path) -> None:
    """The acceptance property: critical sections do not interleave."""
    log = tmp_path / "events.jsonl"
    first = _spawn(database, "index update", timeout=15, hold=0.4, log=log)
    second = _spawn(database, "note update", timeout=15, hold=0.4, log=log)

    assert first.wait(timeout=30) == 0, first.stderr.read()
    assert second.wait(timeout=30) == 0, second.stderr.read()

    events = sorted(_events(log), key=lambda item: item["at"])
    assert [event["kind"] for event in events] == ["enter", "exit", "enter", "exit"]

    spans: dict[str, list[float]] = {}
    for event in events:
        spans.setdefault(event["label"], []).append(event["at"])
    ordered = sorted(spans.values(), key=lambda span: span[0])
    assert ordered[0][1] <= ordered[1][0], f"critical sections overlapped: {spans}"


def test_a_held_lock_makes_the_second_process_fail_fast(database: Path, tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    with vault_lock(database, operation="index rebuild"):
        child = _spawn(database, "note update", timeout=0.2, hold=0.0, log=log)
        assert child.wait(timeout=30) == 3, child.stderr.read()

    assert [event["kind"] for event in _events(log)] == ["busy"]


def test_a_killed_holder_does_not_wedge_the_lock(database: Path, tmp_path: Path) -> None:
    """The kernel drops an flock when the descriptor closes, even on SIGKILL.

    This is why the lock is advisory rather than a pid file: a crashed writer
    must not require manual cleanup before the Vault is usable again.
    """
    log = tmp_path / "events.jsonl"
    holder = _spawn(database, "crashed", timeout=15, hold=30, log=log)
    _wait_for(log, "crashed", "enter")
    holder.kill()
    holder.wait(timeout=30)

    successor = _spawn(database, "successor", timeout=5, hold=0.0, log=log)
    assert successor.wait(timeout=30) == 0, successor.stderr.read()
    assert [event["kind"] for event in _events(log) if event["label"] == "successor"] == [
        "enter",
        "exit",
    ]


def test_lock_file_is_not_deleted_so_inodes_stay_stable(database: Path, tmp_path: Path) -> None:
    """Removing the lock file would let two processes lock different inodes."""
    log = tmp_path / "events.jsonl"
    child = _spawn(database, "solo", timeout=5, hold=0.0, log=log)
    assert child.wait(timeout=30) == 0, child.stderr.read()
    assert lock_path(database).is_file()
