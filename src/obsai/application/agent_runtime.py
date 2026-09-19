"""The bounded agent workflow and the three connections it owns.

The agent needs a metadata index, an artifact store and a LangGraph checkpoint
database at once. The CLI used to open all three inside one function and return a
raw 4-tuple, which made "who closes these" a matter of reading the caller
carefully. :class:`AgentRuntime` owns them and closes them together, and is a
context manager so an exception mid-workflow cannot leak a connection.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from obsai.agent.openai_planner import OpenAIDecisionProvider
from obsai.agent.store import ArtifactStore
from obsai.agent.tools import AgentTools
from obsai.agent.workflow import AgentWorkflow
from obsai.application.answering import build_ask_service, build_hybrid_retriever
from obsai.application.dto import ConsentApproval, SemanticProbe
from obsai.application.search import probe_semantic
from obsai.config.models import Settings
from obsai.storage import Database

ARTIFACTS_SUFFIX = "agent-artifacts.db"
CHECKPOINTS_SUFFIX = "agent-checkpoints.db"


class AgentRuntime:
    """Owns the workflow plus the index, artifact and checkpoint connections."""

    def __init__(
        self,
        workflow: AgentWorkflow,
        database: Database,
        artifacts: ArtifactStore,
        checkpoints: sqlite3.Connection,
    ) -> None:
        self.workflow = workflow
        self.database = database
        self.artifacts = artifacts
        self.checkpoints = checkpoints

    def close(self) -> None:
        """Idempotent: closing twice is not an error, and a half-built runtime closes cleanly."""
        self.checkpoints.close()
        self.artifacts.close()
        self.database.close()

    def __enter__(self) -> "AgentRuntime":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def build_runtime(
    settings: Settings,
    database_path: Path,
    *,
    probe: SemanticProbe | None = None,
    approval: ConsentApproval | None = None,
    query: str = "",
) -> AgentRuntime:
    """Assemble the agent workflow.

    ``probe``/``approval`` carry the remote-embedding decision made by the caller;
    when they are omitted the runtime probes on its own, which means an
    unapproved semantic backend simply degrades rather than calling out.

    Construction is all-or-nothing: any failure after the first connection is
    opened closes everything opened so far before propagating.
    """
    if probe is None:
        with Database(database_path) as probe_database:
            probe = probe_semantic(query, database=probe_database, settings=settings)

    # LangGraph is only needed on this path; importing it here keeps CLI startup
    # cost off the other commands.
    from langgraph.checkpoint.sqlite import SqliteSaver

    database = Database(database_path)
    artifacts: ArtifactStore | None = None
    checkpoints: sqlite3.Connection | None = None
    try:
        retriever = build_hybrid_retriever(database, settings, probe, approval)
        artifacts = ArtifactStore(database_path.with_name(ARTIFACTS_SUFFIX))
        # LangGraph's checkpointer is used from its own threads, so the connection
        # must not be bound to the creating one.
        checkpoints = sqlite3.connect(
            database_path.with_name(CHECKPOINTS_SUFFIX), check_same_thread=False
        )
        checkpointer = SqliteSaver(checkpoints)
        checkpointer.setup()

        answering = build_ask_service(database, settings, retriever)

        def answer_question(question: str):
            answer = answering.ask(question)
            return (
                answer.text,
                [source.record.note_id for source in answer.sources],
                [source.record.chunk_id for source in answer.sources],
            )

        workflow = AgentWorkflow(
            AgentTools(database_path, settings.vault.path, retriever, artifacts),
            artifacts,
            OpenAIDecisionProvider(settings.ask),
            checkpointer=checkpointer,
            answer_question=answer_question,
        )
        return AgentRuntime(workflow, database, artifacts, checkpoints)
    except BaseException:
        if checkpoints is not None:
            checkpoints.close()
        if artifacts is not None:
            artifacts.close()
        database.close()
        raise
