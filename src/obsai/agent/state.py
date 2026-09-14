"""Small checkpoint state: IDs and references, never note bodies or file snapshots."""

from typing import Literal, TypedDict


Intent = Literal[
    "direct_search", "rag_question", "read_operation", "write_operation", "organization_task"
]


class AgentState(TypedDict, total=False):
    query: str
    intent: Intent
    selected_note_ids: list[str]
    retrieved_chunk_ids: list[str]
    tool_result_refs: list[str]
    planned_changes: list[str]
    approved_changes: list[str]
    step_count: int
    retrieval_step_count: int
    error_count: int
    consecutive_error_count: int
    same_tool_call_count: int
    no_progress_count: int
    last_tool_call_hash: str
    last_error_signature: str
    pending_tool: str
    pending_args_ref: str
    final_answer: str
    stop_reason: str
