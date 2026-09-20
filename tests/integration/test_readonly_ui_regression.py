"""B-9 acceptance: the read-only UI stands on its own.

Phase B is the last phase that can be shipped without a write path, so its
regression has to pin the three things that make that true rather than the
features it added. The plan states them as one line:

> 不触碰真实 ``HOME``；无 API key 仍可 keyword 搜索；引用可定位。

Each is a negative — a statement about what does *not* happen — which is exactly
the shape of claim that passes by accident. The rest of this file is what it takes
to make them fail when they are false:

1. **不触碰真实 ``HOME``.** Two checks, because they answer different questions.
   A canary ``HOME`` — config, Vault, index and a pending journal — is pointed at,
   the real read-only entrypoints are run against it, and every byte under it is
   compared afterwards. That proves nothing was written *there*. It does not prove
   nothing was written to the real home as well, so the real home is watched
   separately: a run that read the user's own ``~/.config/obsai`` would still pass
   the first check. An empty home is also checked, because "the CLI never creates
   ``config.toml``" is the same claim from the other end.
2. **无 API key 仍可 keyword 搜索.** Asserted as the *mechanism*: the semantic leg
   is never constructed and the route never even probes. "It returned 200" would
   also be satisfied on a machine that happens to have a key and a network, which
   is precisely the machine where the guarantee is not being tested. The command
   line is checked too, since no spy reaches into a subprocess.
3. **引用可定位.** ``citation.note_id`` has to be an ID ``/notes`` accepts, and
   ``citation.block_id`` has to name a block of the note that comes back. Those
   are the two lookups ``web/src/routes/note.tsx`` performs before it can scroll,
   so "locatable" is their conjunction. The same agreement is pinned for search
   results, which derive their IDs from a different repository.

Nothing here reaches the network or writes to a Vault: the canary is built under
``tmp_path``, the provider is either stubbed or missing its key, and the only
subprocesses are read-only commands.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from obsai.api.app import create_app
from obsai.api.routes import search as search_routes
from obsai.application.search import SEMANTIC_INDEX_MISSING
from obsai.indexing import IncrementalIndexer
from obsai.storage import Database, IndexRepository

REPO = Path(__file__).resolve().parents[2]

#: The console script, not ``python -m obsai``: there is no ``__main__.py``, and
#: the entry point the user actually runs is the one worth regressing.
OBSAI = Path(sys.executable).with_name("obsai")
requires_cli = pytest.mark.skipif(not OBSAI.exists(), reason="console script not installed")

BASE_URL = "http://127.0.0.1:8000"

REDIS = "Backend/Redis.md"
DEPLOY = "Guides/Deploy.md"
TRANSACTION = "deadbeef01234567"

#: Two sections, each a single paragraph carrying its own ``^anchor``. That is what
#: makes ``chunk.block_id`` non-null (see ``obsai.chunking.chunker``: a chunk only
#: inherits an anchor when it holds exactly one unit), which is what the citation
#: test needs in order to exercise the block lookup at all.
REDIS_NOTE = """---
tags: [redis, cache]
---
# Redis

Redis 是内存缓存，用于加速读取。 ^cache

## 配置

把上限写进 redis.conf 更省事。 ^maxmemory
"""

DEPLOY_NOTE = "# Deploy\n\nSee [[Backend/Redis]].\n"


# --------------------------------------------------------------------------- #
# A canary HOME
# --------------------------------------------------------------------------- #


def write(vault: Path, path: str, content: str) -> Path:
    target = vault / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def unfinished_journal(vault: Path) -> Path:
    """A journal that makes ``/status`` report ``recovery_required``.

    Hand-written rather than produced by a genuinely interrupted transaction:
    provoking a real crash mid-commit is a different test, and all this one needs
    is a journal for the status read to *find*. Without it, ``/status`` would do
    less work and the byte-comparison would be checking a smaller surface.
    """
    directory = vault / ".obsai-transactions" / TRANSACTION
    (directory / "snapshots").mkdir(parents=True)
    (directory / "journal.json").write_text(
        json.dumps(
            {
                "id": TRANSACTION,
                "status": "prepared",
                "applied_count": 0,
                "originals": [{"path": REDIS, "hash": None, "snapshot": None, "mode": 420}],
                "changes": [],
                "absent_directories": [],
                "dirty_paths": [],
                "error": None,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return directory


def canary_home(tmp_path: Path) -> Path:
    """A complete, self-contained HOME: config, Vault, index and a pending journal.

    Everything a read-only entrypoint could conceivably need is here, so a write
    would have somewhere to land. An empty directory would pass the comparison
    below for the wrong reason.
    """
    home = tmp_path / "canary-home"
    vault = home / "vault"
    vault.mkdir(parents=True)
    write(vault, REDIS, REDIS_NOTE)
    write(vault, DEPLOY, DEPLOY_NOTE)

    database = home / "index.db"
    with Database(database) as connection:
        IncrementalIndexer(IndexRepository(connection)).update(vault)

    config = home / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f'[vault]\npath = "{vault}"\n[index]\ndatabase = "{database}"\n', encoding="utf-8"
    )
    unfinished_journal(vault)
    return home


def snapshot(root: Path) -> dict[str, str]:
    """Every entry under ``root``, by relative path, with each file's digest.

    Directories are recorded as well: a journal directory that appeared but is
    still empty is a change, and hashing files alone would not see it.
    """
    entries: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            entries[relative] = f"symlink -> {os.readlink(path)}"
        elif path.is_dir():
            entries[relative] = "dir"
        else:
            entries[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return entries


def environment(home: Path) -> dict[str, str]:
    """The canary HOME, and nothing that could reach a provider."""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / "config")
    env["COLUMNS"] = "80"
    env.pop("OPENAI_API_KEY", None)
    for name in [key for key in env if key.startswith("OBSAI_")]:
        env.pop(name)
    config = home / "config" / "obsai" / "config.toml"
    if config.exists():
        env["OBSAI_CONFIG_PATH"] = str(config)
    return env


def run_cli(home: Path, argv: list[str]) -> subprocess.CompletedProcess:
    """Run one command as the user would, against the canary.

    ``input=""`` rather than inheriting a terminal: every command below is
    read-only, so no prompt should be reached, and a closed stdin turns "a prompt
    appeared" into a deterministic abort instead of a hang.
    """
    return subprocess.run(
        [str(OBSAI), *argv],
        cwd=home,
        env=environment(home),
        input="",
        capture_output=True,
        text=True,
        timeout=120,
    )


def point_at(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Make in-process code read the canary, exactly as it reads the real one.

    ``create_app()`` is used without a ``get_settings`` override so the whole
    dependency path — loader, defaults, index resolution — is the real one.
    """
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))


def flatten(text: str) -> str:
    """Rich wraps at ``COLUMNS``; compare on the message, not the layout."""
    return " ".join(text.split())


def unwrap(text: str) -> str:
    """Drop every space, for a token Rich can break *inside*.

    ``flatten`` is enough for a sentence: a fold lands on a word boundary and the
    join puts a space back where the newline was. A long path has no word
    boundaries, so Rich folds it mid-token — ``.../canary-home/va`` newline ``ult``
    — and a space-join yields ``va ult``, which never matches the real path. The
    assertion then silently depends on ``TMPDIR`` being short, which is exactly how
    it broke once the basetemp name grew by four characters.
    """
    return "".join(text.split())


def note_id_of(database_path: Path, path: str) -> str:
    with Database(database_path) as database:
        record = IndexRepository(database).notes.get_by_path(path)
    assert record is not None, f"{path} 没有被索引到，后面的断言都会失去意义"
    return record.id


# --------------------------------------------------------------------------- #
# 1. 不触碰真实 HOME
# --------------------------------------------------------------------------- #


@requires_cli
def test_the_read_only_cli_entries_work_against_the_canary(tmp_path: Path) -> None:
    """The precondition for the byte-comparison: these commands actually ran.

    Without this, a mistyped argument list would make the next test pass because
    nothing executed — the failure mode every "nothing changed" assertion has.
    """
    home = canary_home(tmp_path)

    status = run_cli(home, ["status"])
    assert status.returncode == 0, status.stderr
    assert str(home / "vault") in unwrap(status.stdout)

    found = run_cli(home, ["search", "redis", "--mode", "keyword"])
    assert found.returncode == 0, found.stderr
    assert REDIS in found.stdout

    pending = run_cli(home, ["transaction", "status"])
    assert pending.returncode == 0, pending.stderr
    assert TRANSACTION in pending.stdout

    links = run_cli(home, ["links", "backlinks", REDIS])
    assert links.returncode == 0, links.stderr
    assert DEPLOY in links.stdout


@requires_cli
def test_the_read_only_cli_leaves_the_canary_home_byte_identical(tmp_path: Path) -> None:
    """The whole read-only command surface, measured rather than assumed.

    ``ask`` is in the list even though it fails without a key: a command that
    errors is still a command that ran, and the interesting question is whether it
    left anything behind on its way to the error.
    """
    home = canary_home(tmp_path)
    before = snapshot(home)

    for argv in (
        ["status"],
        ["search", "redis", "--mode", "keyword"],
        ["search", "redis", "--mode", "hybrid"],
        ["transaction", "status"],
        ["links", "backlinks", REDIS],
        ["links", "outgoing", DEPLOY],
        ["ask", "redis 是什么"],
    ):
        run_cli(home, argv)

    assert snapshot(home) == before


def test_the_read_only_endpoints_leave_the_canary_home_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same claim through the HTTP adapter, with no dependency overrides.

    ``/ask`` is expected to fail here — there is no key — and the status is
    asserted so that a 500 from something unrelated cannot be mistaken for the
    expected refusal.
    """
    home = canary_home(tmp_path)
    point_at(monkeypatch, home)
    # Resolved before the snapshot: this opens the index itself, and the question
    # under test is what the *endpoints* leave behind.
    note_id = note_id_of(home / "index.db", REDIS)
    before = snapshot(home)

    with TestClient(create_app(), base_url=BASE_URL) as client:
        assert client.get("/api/v1/status").status_code == 200
        assert client.post(
            "/api/v1/search", json={"query": "redis", "mode": "keyword"}
        ).status_code == 200
        assert client.get(f"/api/v1/notes/{note_id}").status_code == 200
        assert client.post("/api/v1/ask", json={"query": "redis"}).status_code == 400

    assert snapshot(home) == before


def real_home() -> Path | None:
    """The user's actual home directory, read from the password database.

    ``HOME`` cannot be used: the autouse fixture in ``tests/conftest.py`` replaces
    it for every test, which is the isolation this file is checking. The passwd
    entry is not affected by the environment, so it still points at the real one.
    """
    try:
        import pwd
    except ImportError:  # pragma: no cover - Windows has no pwd
        return None
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:  # pragma: no cover - uid with no passwd entry
        return None


#: The two places an ObsAgent run could write under a user's home. The config is
#: what the CLI reads; ``~/.obsai`` is where the index defaults to when no
#: ``[index] database`` is configured.
WATCHED = (".config/obsai", ".obsai")


def watched(home: Path) -> dict[str, dict[str, str] | None]:
    """The watched directories as they are now, or ``None`` when absent.

    ``None`` rather than ``{}``: "the directory does not exist" and "the directory
    exists and is empty" are different states, and only one of them means
    something created it.
    """
    return {
        relative: snapshot(home / relative) if (home / relative).is_dir() else None
        for relative in WATCHED
    }


def test_the_real_home_is_neither_read_from_nor_written_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half of the claim a canary HOME cannot make on its own.

    Pointing at a canary proves the entrypoints only touch what they were pointed
    at. It does not prove they were not *also* pointed at the real home — a
    hardcoded ``Path.home()`` would sail through every test above, because
    ``Path.home()`` honours ``HOME``. So the real one is watched directly, and
    both directions are covered: a config that exists must not change, and one
    that does not must not appear.
    """
    home = real_home()
    if home is None:
        pytest.skip("no password database entry for this uid")

    before = watched(home)

    canary = canary_home(tmp_path)
    run_cli(canary, ["status"])
    run_cli(canary, ["search", "redis", "--mode", "keyword"])
    point_at(monkeypatch, canary)
    with TestClient(create_app(), base_url=BASE_URL) as client:
        assert client.get("/api/v1/status").status_code == 200
        assert client.post(
            "/api/v1/search", json={"query": "redis", "mode": "keyword"}
        ).status_code == 200

    assert watched(home) == before, (
        f"只读入口改动了真实 HOME 下的 {WATCHED} —— 测试环境隔离漏了某条路径"
    )


@requires_cli
def test_an_empty_home_gains_no_configuration(tmp_path: Path, monkeypatch) -> None:
    """The other end of the same claim: reading configuration does not create it.

    A user who has never run ``obsai`` has no ``config.toml``. Launching the UI
    must not manufacture one — an empty file that looks like a configuration is
    worse than a missing one, because the next command reads it and reports
    success.
    """
    empty = tmp_path / "empty-home"
    empty.mkdir()
    monkeypatch.setenv("HOME", str(empty))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty / "config"))

    status = run_cli(empty, ["status"])
    assert status.returncode == 0, status.stderr
    assert "No vault configured" in status.stdout

    # ``/status`` is total, so this is a 200 that says "no vault" rather than a
    # 400 — and either way it must not have written anything.
    with TestClient(create_app(), base_url=BASE_URL) as client:
        response = client.get("/api/v1/status")
    assert response.status_code == 200, response.json()
    assert response.json()["vault_path"] is None
    assert response.json()["vault_ready"] is False

    assert snapshot(empty) == {}


# --------------------------------------------------------------------------- #
# 2. 无 API key 仍可 keyword 搜索
# --------------------------------------------------------------------------- #


def test_keyword_search_never_probes_and_never_builds_an_embedding_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mechanism behind the criterion, rather than its outcome.

    Two spies, because they guard different code. The route-level one records
    whether a probe happened at all — ``probe_semantic`` swallows every exception
    on its non-strict path, so a spy that only raised inside it would be turned
    into a degradation notice instead of a failure. The module-level one proves
    ``search`` itself returns before it reaches for a pipeline.
    """
    home = canary_home(tmp_path)
    point_at(monkeypatch, home)

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("keyword 模式不该构造嵌入流水线")

    monkeypatch.setattr("obsai.application.search.build_embedding_pipeline", explode)
    monkeypatch.setattr("obsai.application.search.build_semantic_retriever", explode)

    probed: list[str] = []
    real_probe = search_routes.probe_semantic

    def spy(query: str, **kwargs: object):
        probed.append(query)
        return real_probe(query, **kwargs)

    monkeypatch.setattr(search_routes, "probe_semantic", spy)

    with TestClient(create_app(), base_url=BASE_URL) as client:
        response = client.post("/api/v1/search", json={"query": "redis", "mode": "keyword"})

    assert response.status_code == 200, response.json()
    payload = response.json()
    # Two chunks of ``Redis.md`` match "redis" and ``Deploy.md`` links to it, so the
    # set is asserted rather than the order: this test is about the semantic leg not
    # being built, and pinning the ranking here would make it fail for a reason it
    # is not about.
    assert {result["path"] for result in payload["results"]} == {REDIS, DEPLOY}
    assert probed == [], "keyword 模式探测了语义后端 —— 它就依赖上嵌入配置了"
    # The probe field is ``None`` rather than an empty probe: nothing asked.
    assert payload["semantic"] is None
    assert payload["warnings"] == []


@requires_cli
def test_keyword_search_works_on_the_command_line_with_no_key(tmp_path: Path) -> None:
    """The same claim where no spy can reach, with the environment scrubbed.

    ``environment`` deletes ``OPENAI_API_KEY`` and every ``OBSAI_*`` override, so
    a machine with a real key in its profile tests the same thing this does. The
    empty ``warnings`` is the assertion that matters: a degradation notice would
    mean the semantic leg was attempted after all.
    """
    home = canary_home(tmp_path)
    result = run_cli(home, ["search", "redis", "--mode", "keyword"])

    assert result.returncode == 0, result.stderr
    assert REDIS in result.stdout
    assert "Warning" not in result.stderr, result.stderr


@requires_cli
def test_semantic_mode_refuses_instead_of_pretending(tmp_path: Path) -> None:
    """The guard that makes the criterion meaningful rather than convenient.

    Without vectors there is nothing semantic to return. Falling back quietly
    would hand the caller keyword results under a semantic label — worse than the
    failure, because the failure is readable and the fallback is not.
    """
    home = canary_home(tmp_path)
    result = run_cli(home, ["search", "redis", "--mode", "semantic"])

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert SEMANTIC_INDEX_MISSING in flatten(result.stderr)
    assert REDIS not in result.stdout, "拒绝之后不能把关键词结果当成语义结果交出去"


# --------------------------------------------------------------------------- #
# 3. 引用可定位
# --------------------------------------------------------------------------- #


def block_ids(blocks: list[dict]) -> set[str]:
    return {block["block_id"] for block in blocks if block["block_id"] is not None}


def heading_titles(blocks: list[dict]) -> set[str]:
    """The text of every heading block, which is what ``heading_path`` holds."""
    return {
        "".join(segment["text"] for segment in block["segments"])
        for block in blocks
        if block["kind"] == "heading"
    }


def test_every_citation_resolves_to_a_note_and_a_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_llm
) -> None:
    """引用可定位, stated as the two lookups the note page performs.

    ``web/src/lib/note.ts::blockIndexFor`` scrolls only when the ``?block=`` value
    names a block of the note that came back, and ``noteHref`` builds that value
    from ``citation.block_id``. So "locatable" is a conjunction across two
    endpoints, and the failure it guards against is silent: a citation whose ID
    resolves but whose anchor does not lands the reader at the top of the note with
    nothing to indicate the target was missed.

    A citation with no anchor is not a failure — ``[[Note]]``-style evidence has no
    position to point at, and opening the note is the honest behaviour. Those are
    required to carry a heading path instead, so that "there is a position" is
    still true in the weaker form.
    """
    home = canary_home(tmp_path)
    point_at(monkeypatch, home)
    stub_llm("Redis 是内存缓存 [S1]，配置上限见 [S2]。")

    with TestClient(create_app(), base_url=BASE_URL) as client:
        response = client.post("/api/v1/ask", json={"query": "redis"})
        assert response.status_code == 200, response.json()
        answer = response.json()
        assert answer["abstained"] is False, answer
        assert answer["citations"], "stub 引用了 [S1][S2]，引用列表不该是空的"

        for citation in answer["citations"]:
            note = client.get(f"/api/v1/notes/{citation['note_id']}")
            assert note.status_code == 200, citation
            view = note.json()
            assert view["note_id"] == citation["note_id"]
            assert view["path"] == citation["path"]

            if citation["block_id"] is None:
                assert citation["heading_path"], "没有块锚点的引用至少要指明标题"
                assert citation["heading_path"][-1] in heading_titles(view["blocks"])
            else:
                assert citation["block_id"] in block_ids(view["blocks"]), (
                    f"{citation['block_id']} 不在 {view['path']} 的块里 —— "
                    "引用指向了不存在的位置，页面会静默停在顶部"
                )

    assert any(item["block_id"] is not None for item in answer["citations"]), (
        "所有引用都没有块锚点，上面的块查找等于没跑 —— 换一篇带 ^锚点 的笔记"
    )


def test_a_search_result_leads_to_the_note_it_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same promise: a result card is not a dead end.

    ``/search`` and ``/notes`` derive their IDs independently — one from the
    retrieval repository, one from the note repository — so "the ID a result
    carries is an ID the note endpoint accepts" is an agreement between two
    endpoints rather than a tautology. The 404 at the end keeps it honest: it
    shows the endpoint can refuse, so the 200s above are not unconditional.
    """
    home = canary_home(tmp_path)
    point_at(monkeypatch, home)

    with TestClient(create_app(), base_url=BASE_URL) as client:
        response = client.post("/api/v1/search", json={"query": "redis", "mode": "keyword"})
        assert response.status_code == 200, response.json()
        results = response.json()["results"]
        assert results, "canary 里没有命中，后面的循环等于没跑"

        for result in results:
            note = client.get(f"/api/v1/notes/{result['note_id']}")
            assert note.status_code == 200, result
            assert note.json()["path"] == result["path"]

        assert client.get("/api/v1/notes/0000000000000000").status_code == 404
