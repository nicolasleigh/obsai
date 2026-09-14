"""Agent stays bounded and never applies a write before checkpoint approval."""

import sqlite3

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from obsai.agent.store import ArtifactStore
from obsai.agent.tools import AgentTools
from obsai.agent.workflow import AgentLimits, AgentWorkflow, ToolDecision
from obsai.storage import Database


class Planner:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.calls = 0

    def decide(self, **kwargs):
        self.calls += 1
        return next(self.decisions)


class FakeTools:
    def __init__(self, artifacts, *, fail=False):
        self.artifacts = artifacts
        self.fail = fail
        self.read_calls = 0
        self.applied = 0

    def read(self, name, args):
        self.read_calls += 1
        if self.fail:
            raise ValueError("bad path")
        return self.artifacts.put([{"example": "evidence"}]), [f"note-{self.read_calls}"], [
            f"chunk-{self.read_calls}"], f"Found evidence {self.read_calls}"

    def plan_write(self, name, args):
        return self.artifacts.put({"name": name, "args": args}), "--- old\n+++ new\n+approved content"

    def apply_write(self, ref):
        self.applied += 1
        assert self.artifacts.get(ref)["name"] == "create_note"
        return type("Result", (), {"index_dirty": False})()


def workflow(tmp_path, decisions, *, fail=False, limits=AgentLimits(), checkpointer=None):
    artifacts = ArtifactStore(tmp_path / "artifacts.db")
    tools = FakeTools(artifacts, fail=fail)
    planner = Planner(decisions)
    agent = AgentWorkflow(tools, artifacts, planner, checkpointer=checkpointer or InMemorySaver(),
                          answer_question=lambda query: ("Evidence answer [S1]", ["n"], ["c"]),
                          limits=limits)
    return agent, tools, planner, artifacts


def test_direct_search_bypasses_planner(tmp_path):
    agent, tools, planner, store = workflow(tmp_path, [])
    result = agent.run("search context", "direct")
    assert result["retrieved_chunk_ids"] == ["chunk-1"]
    assert tools.read_calls == 1
    assert planner.calls == 0
    store.close()


def test_rag_route_reuses_answer_service_without_planning_loop(tmp_path):
    agent, tools, planner, store = workflow(tmp_path, [])
    result = agent.run("我以前如何理解 graceful shutdown？", "rag")
    assert result["final_answer"] == "Evidence answer [S1]"
    assert result["retrieved_chunk_ids"] == ["c"]
    assert tools.read_calls == 0
    assert planner.calls == 0
    store.close()


def test_two_retrievals_keep_only_references(tmp_path):
    decisions = [ToolDecision("search_notes", {"query": "one"}),
                 ToolDecision("search_notes", {"query": "two"}),
                 ToolDecision(final_answer="Found two notes")]
    agent, tools, _, store = workflow(tmp_path, decisions)
    result = agent.run("查看笔记 context", "two")
    assert result["retrieval_step_count"] == 2
    assert result["retrieved_chunk_ids"] == ["chunk-1", "chunk-2"]
    assert result["final_answer"] == "Found two notes"
    assert "evidence" not in str(result)
    store.close()


def test_bad_path_loop_stops_on_repeated_error(tmp_path):
    decisions = [ToolDecision("read_note", {"note_id": "bad"})] * 10
    agent, tools, _, store = workflow(tmp_path, decisions, fail=True)
    result = agent.run("查看笔记 bad", "bad")
    assert "Repeated invalid tool calls" in result["final_answer"]
    assert tools.read_calls <= 2
    store.close()


def test_same_call_and_step_limits(tmp_path):
    same = [ToolDecision("search_notes", {"query": "same"})] * 10
    agent, tools, _, store = workflow(tmp_path, same)
    result = agent.run("查看笔记 same", "same")
    assert "Repeated identical tool call" in result["final_answer"]
    assert tools.read_calls == 2
    store.close()
    agent, tools, _, store = workflow(tmp_path / "steps", [
        ToolDecision("search_notes", {"query": str(i)}) for i in range(10)
    ], limits=AgentLimits(max_steps=2))
    result = agent.run("查看笔记 steps", "steps")
    assert "Maximum steps reached" in result["final_answer"]
    assert tools.read_calls == 2
    store.close()


def test_write_rejection_and_approval(tmp_path):
    decision = ToolDecision("create_note", {"path": "new.md", "content": "hello"})
    agent, tools, _, store = workflow(tmp_path, [decision])
    pending = agent.run("创建 new.md", "reject")
    assert pending["__interrupt__"][0].value["kind"] == "write_approval"
    assert "preview" not in pending["__interrupt__"][0].value
    assert "hello" not in str(agent.graph.get_state({"configurable": {"thread_id": "reject"}}).values)
    assert tools.applied == 0
    rejected = agent.resume("reject", approved=False)
    assert rejected["final_answer"] == "Write cancelled by user."
    assert tools.applied == 0
    agent2, tools2, _, _ = workflow(tmp_path / "approved", [decision])
    pending = agent2.run("创建 new.md", "approve")
    assert pending["__interrupt__"]
    accepted = agent2.resume("approve", approved=True)
    assert accepted["final_answer"] == "Change applied."
    assert tools2.applied == 1
    store.close()


def test_sqlite_checkpoint_resume_across_workflow_instances(tmp_path):
    checkpoint_path = tmp_path / "checkpoint.db"
    connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
    saver = SqliteSaver(connection)
    saver.setup()
    decision = ToolDecision("create_note", {"path": "new.md", "content": "hello"})
    agent, tools, _, store = workflow(tmp_path, [decision], checkpointer=saver)
    agent.run("创建 new.md", "durable")
    connection.close()
    store.close()

    new_connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
    new_saver = SqliteSaver(new_connection)
    new_store = ArtifactStore(tmp_path / "artifacts.db")
    new_tools = FakeTools(new_store)
    resumed = AgentWorkflow(new_tools, new_store, Planner([]), checkpointer=new_saver)
    assert resumed.resume("durable", approved=True)["final_answer"] == "Change applied."
    assert new_tools.applied == 1
    new_connection.close()
    new_store.close()


def test_retrieval_limit_and_invalid_tool_stop(tmp_path):
    decisions = [ToolDecision("search_notes", {"query": str(i)}) for i in range(10)]
    agent, tools, _, store = workflow(tmp_path, decisions,
                                      limits=AgentLimits(max_retrieval_steps=2))
    result = agent.run("查看笔记 context", "retrieval")
    assert "Maximum retrieval steps reached" in result["final_answer"]
    assert tools.read_calls == 2
    store.close()
    agent, _, planner, store = workflow(tmp_path / "invalid", [ToolDecision("bad_tool", {})] * 10)
    result = agent.run("查看笔记 context", "invalid")
    assert "could not safely continue" in result["final_answer"]
    assert planner.calls <= 4
    store.close()


def test_no_progress_breaker(tmp_path):
    agent, _, _, store = workflow(
        tmp_path, [ToolDecision("invalid", {})] * 10,
        limits=AgentLimits(max_consecutive_errors=10, max_no_progress_steps=2),
    )
    result = agent.run("查看笔记 context", "no-progress")
    assert "No progress" in result["final_answer"]
    store.close()


def test_real_transaction_tool_requires_approval_and_uses_occ(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    database_path = tmp_path / "index.db"
    with Database(database_path):
        pass
    artifacts = ArtifactStore(tmp_path / "artifacts.db")
    tools = AgentTools(database_path, vault, retriever=None, artifacts=artifacts)
    plan_ref, preview = tools.plan_write("create_note", {"path": "new.md", "content": "hello"})
    assert "hello" in preview
    assert not (vault / "new.md").exists()
    assert tools.apply_write(plan_ref).committed
    assert (vault / "new.md").read_text() == "hello"
    update_ref, _ = tools.plan_write("update_note", {"path": "new.md", "old": "hello", "new": "bye"})
    (vault / "new.md").write_text("external edit")
    from obsai.errors import ConflictError
    import pytest
    with pytest.raises(ConflictError):
        tools.apply_write(update_ref)
    assert (vault / "new.md").read_text() == "external edit"
    artifacts.close()
