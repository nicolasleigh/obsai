from typer.testing import CliRunner

from obsai.agent.openai_planner import OpenAIDecisionProvider
from obsai.agent.workflow import ToolDecision
from obsai.cli.app import app


def test_agent_cli_search_and_checkpointed_write(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Context.md").write_text("# Context\n\nGraceful shutdown waits.\n")
    database = tmp_path / "index.db"
    config = tmp_path / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(f'[vault]\npath = "{vault}"\n[index]\ndatabase = "{database}"\n')
    runner = CliRunner()
    assert runner.invoke(app, ["index", "update"]).exit_code == 0

    def forbidden(*args, **kwargs):
        raise AssertionError("Direct search should not call the planner")

    monkeypatch.setattr(OpenAIDecisionProvider, "decide", forbidden)
    found = runner.invoke(app, ["agent", "run", "搜索 Graceful", "--thread-id", "search"])
    assert found.exit_code == 0, found.output
    assert "Context.md" in found.output

    monkeypatch.setattr(OpenAIDecisionProvider, "decide", lambda self, **kwargs:
                        ToolDecision("create_note", {"path": "New.md", "content": "# New"}))
    pending = runner.invoke(app, ["agent", "run", "创建 New.md", "--thread-id", "write-1"])
    assert pending.exit_code == 0, pending.output
    assert "Approval required" in pending.output
    assert not (vault / "New.md").exists()
    rejected = runner.invoke(app, ["agent", "resume", "write-1"], input="n\n")
    assert rejected.exit_code == 0, rejected.output
    assert "Write cancelled" in rejected.output
    assert not (vault / "New.md").exists()

    pending = runner.invoke(app, ["agent", "run", "创建 New.md", "--thread-id", "write-2"])
    assert pending.exit_code == 0, pending.output
    approved = runner.invoke(app, ["agent", "resume", "write-2"], input="y\n")
    assert approved.exit_code == 0, approved.output
    assert "Change applied" in approved.output
    assert (vault / "New.md").read_text() == "# New"
