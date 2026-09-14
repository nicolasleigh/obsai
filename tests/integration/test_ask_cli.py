from pathlib import Path

from typer.testing import CliRunner

from obsai.answering.openai_provider import OpenAILLMProvider
from obsai.cli.app import app
from obsai.storage import Database, IndexRepository, SQLiteEvidenceRepository


def test_ask_cli_uses_indexed_evidence_and_cites_source(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Go").mkdir()
    (vault / "Go" / "context.md").write_text(
        "# Go Context\n\n## Graceful Shutdown\n\nGraceful shutdown waits for requests.\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "index.db"
    config = tmp_path / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f'[vault]\npath = "{vault}"\n[index]\ndatabase = "{db_path}"\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    assert runner.invoke(app, ["index", "update"]).exit_code == 0
    (vault / "Go" / "context.md").rename(vault / "Go" / "renamed.md")
    assert runner.invoke(app, ["index", "update"]).exit_code == 0

    async def fake_generate(self, system_prompt: str, user_prompt: str, *, max_output_tokens: int) -> str:
        assert "Graceful shutdown waits for requests." in user_prompt
        assert "Go/renamed.md" in user_prompt
        assert "[S1]" in user_prompt
        return "It waits for requests to finish. [S1]"

    monkeypatch.setattr(OpenAILLMProvider, "generate", fake_generate)
    answer = runner.invoke(app, ["ask", "Graceful shutdown"])
    assert answer.exit_code == 0, answer.output
    assert "It waits for requests to finish. [S1]" in answer.output
    assert "Sources:" in answer.output
    assert "Go/renamed.md > Go Context > Graceful Shutdown" in answer.output
    with Database(db_path) as database:
        note = IndexRepository(database).notes.get_by_path("Go/renamed.md")
        chunk = IndexRepository(database).chunks.list_for_note(note.id)[0]
        evidence = SQLiteEvidenceRepository(database).get(chunk.chunk_id)
        assert evidence.path == "Go/renamed.md"


def test_ask_cli_without_results_does_not_call_provider(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "index.db"
    with Database(db_path):
        pass
    config = tmp_path / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(f'[index]\ndatabase = "{db_path}"\n', encoding="utf-8")

    async def forbidden(*args, **kwargs):
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(OpenAILLMProvider, "generate", forbidden)
    answer = CliRunner().invoke(app, ["ask", "Anything"])
    assert answer.exit_code == 0, answer.output
    assert "未找到可用于回答的笔记证据" in answer.output
