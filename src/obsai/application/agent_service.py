"""Application service for running, inspecting and resuming agent workflows.

Encapsulates LangGraph runtime lifecycle, checkpointer inspection and
human-in-the-loop approval resume so the API layer never reaches past
the application boundary.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from obsai.application.agent_runtime import AgentRuntime, build_runtime
from obsai.application.dto import (
    AgentApprovalView,
    AgentRunView,
    AgentTimelineItem,
)
from obsai.application.paths import require_index, require_vault
from obsai.config.models import Settings
from obsai.errors import ConfigError, ConflictError, NotFoundError


def build_agent_run_view(
    runtime: AgentRuntime,
    run_id: str,
    state_values: dict[str, Any],
    tasks: tuple[Any, ...] | list[Any] = (),
) -> AgentRunView:
    """Translate raw checkpoint state and interrupts into the wire contract."""
    query = str(state_values.get("query", ""))
    step_count = int(state_values.get("step_count", 0))
    retrieval_step_count = int(state_values.get("retrieval_step_count", 0))
    selected_note_ids = tuple(str(x) for x in state_values.get("selected_note_ids", ()))
    retrieved_chunk_ids = tuple(str(x) for x in state_values.get("retrieved_chunk_ids", ()))
    final_answer = state_values.get("final_answer")
    stop_reason = state_values.get("stop_reason")

    timeline_items: list[AgentTimelineItem] = []
    for ref in state_values.get("tool_result_refs", ()):
        try:
            payload = runtime.artifacts.get(ref)
            if isinstance(payload, dict):
                timeline_items.append(
                    AgentTimelineItem(
                        tool=str(payload.get("tool", "unknown")),
                        summary=str(payload.get("summary", "")),
                        args=payload.get("args") if isinstance(payload.get("args"), dict) else {},
                    )
                )
            elif isinstance(payload, list):
                timeline_items.append(
                    AgentTimelineItem(
                        tool="search_notes",
                        summary=f"Search returned {len(payload)} results",
                        args={},
                    )
                )
        except Exception:
            continue

    # Determine pending interrupt from Pregel tasks or state payload
    interrupt_payload = None
    if tasks and getattr(tasks[0], "interrupts", None):
        interrupt_payload = tasks[0].interrupts[0].value
    elif state_values.get("__interrupt__"):
        interrupt_payload = state_values["__interrupt__"][0].value

    pending_approval: AgentApprovalView | None = None
    if interrupt_payload is not None:
        kind = str(interrupt_payload.get("kind", "write_approval"))
        plan_ref = interrupt_payload.get("plan_ref")
        preview_ref = interrupt_payload.get("preview_ref")
        preview_text = ""
        if preview_ref:
            try:
                preview_item = runtime.artifacts.get(preview_ref)
                if isinstance(preview_item, dict):
                    preview_text = str(preview_item.get("preview", ""))
            except Exception:
                pass
        pending_approval = AgentApprovalView(
            kind=kind,
            preview=preview_text,
            plan_ref=plan_ref,
        )
        status = "interrupted"
    else:
        if stop_reason:
            status = "failed"
        else:
            status = "completed"

    return AgentRunView(
        run_id=run_id,
        status=status,
        query=query,
        step_count=step_count,
        retrieval_step_count=retrieval_step_count,
        selected_note_ids=selected_note_ids,
        retrieved_chunk_ids=retrieved_chunk_ids,
        timeline=tuple(timeline_items),
        final_answer=final_answer,
        stop_reason=stop_reason,
        pending_approval=pending_approval,
    )


def start_agent_run(
    settings: Settings,
    query: str,
    thread_id: str | None = None,
) -> AgentRunView:
    """Create and start a workflow run. Raises ConflictError if thread_id exists."""
    require_vault(settings)
    index_path = require_index(settings)
    run_id = thread_id or uuid4().hex
    with build_runtime(settings, index_path, query=query) as runtime:
        if thread_id is not None:
            snapshot = runtime.workflow.graph.get_state({"configurable": {"thread_id": thread_id}})
            if snapshot.values:
                raise ConflictError(f"Workflow {thread_id} already exists")
        result = runtime.workflow.run(query, run_id)
        snapshot = runtime.workflow.graph.get_state({"configurable": {"thread_id": run_id}})
        return build_agent_run_view(
            runtime,
            run_id,
            snapshot.values or result,
            getattr(snapshot, "tasks", ()),
        )


def get_agent_run(settings: Settings, run_id: str) -> AgentRunView:
    """Retrieve existing workflow execution state from LangGraph checkpoint."""
    require_vault(settings)
    index_path = require_index(settings)
    with build_runtime(settings, index_path) as runtime:
        snapshot = runtime.workflow.graph.get_state({"configurable": {"thread_id": run_id}})
        if not snapshot.values:
            raise NotFoundError(f"Workflow {run_id} not found")
        return build_agent_run_view(
            runtime,
            run_id,
            snapshot.values,
            getattr(snapshot, "tasks", ()),
        )


def resume_agent_run(
    settings: Settings,
    run_id: str,
    *,
    approved: bool = True,
) -> AgentRunView:
    """Resume an interrupted write workflow following human approval or refusal."""
    require_vault(settings)
    index_path = require_index(settings)
    with build_runtime(settings, index_path) as runtime:
        snapshot = runtime.workflow.graph.get_state({"configurable": {"thread_id": run_id}})
        if not snapshot.values:
            raise NotFoundError(f"Workflow {run_id} not found")
        if not snapshot.tasks or not snapshot.tasks[0].interrupts:
            raise ConfigError(f"Workflow {run_id} is not awaiting approval")
        result = runtime.workflow.resume(run_id, approved=approved)
        new_snapshot = runtime.workflow.graph.get_state({"configurable": {"thread_id": run_id}})
        return build_agent_run_view(
            runtime,
            run_id,
            new_snapshot.values or result,
            getattr(new_snapshot, "tasks", ()),
        )
