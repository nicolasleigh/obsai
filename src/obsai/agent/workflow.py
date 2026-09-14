"""Bounded LangGraph workflow with reference-only state and explicit write HITL."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from obsai.agent.router import route_intent
from obsai.agent.state import AgentState
from obsai.agent.store import ArtifactStore
from obsai.agent.tools import ALL_TOOLS, READ_TOOLS, WRITE_TOOLS, AgentTools
from obsai.errors import ConfigError
from obsai.shutdown import check_shutdown
from obsai.telemetry import measure, metric


FALLBACK = "Agent could not safely continue.\n\nReason: "


@dataclass(frozen=True)
class AgentLimits:
    max_steps: int = 15
    max_retrieval_steps: int = 5
    max_consecutive_errors: int = 3
    max_same_tool_call: int = 2
    max_no_progress_steps: int = 3


@dataclass(frozen=True)
class ToolDecision:
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    final_answer: str | None = None


class DecisionProvider(Protocol):
    def decide(self, *, query: str, intent: str, recent_summaries: list[str],
               available_tools: tuple[str, ...]) -> ToolDecision: ...


class AgentWorkflow:
    def __init__(self, tools: AgentTools, artifacts: ArtifactStore,
                 planner: DecisionProvider, *, checkpointer: Any,
                 answer_question: Callable[[str], tuple[str, list[str], list[str]]] | None = None,
                 limits: AgentLimits = AgentLimits()):
        self.tools = tools
        self.artifacts = artifacts
        self.planner = planner
        self.answer_question = answer_question
        self.limits = limits
        graph = StateGraph(AgentState)
        graph.add_node("route", self._route)
        graph.add_node("direct_search", self._direct_search)
        graph.add_node("rag", self._rag)
        graph.add_node("decide", self._decide)
        graph.add_node("execute_read", self._execute_read)
        graph.add_node("plan_write", self._plan_write)
        graph.add_node("approve", self._approve)
        graph.add_node("apply_write", self._apply_write)
        graph.add_edge(START, "route")
        graph.add_conditional_edges("route", lambda state: (
            "direct_search" if state["intent"] == "direct_search"
            else "rag" if state["intent"] == "rag_question" else "decide"
        ))
        graph.add_edge("direct_search", END)
        graph.add_edge("rag", END)
        graph.add_conditional_edges("decide", self._after_decision)
        graph.add_edge("execute_read", "decide")
        graph.add_conditional_edges("plan_write", lambda state: "approve" if state.get("planned_changes") else "decide")
        graph.add_conditional_edges("approve", lambda state: "apply_write" if state.get("approved_changes") else END)
        graph.add_edge("apply_write", END)
        self.graph = graph.compile(checkpointer=checkpointer)

    @staticmethod
    def _initial(query: str) -> AgentState:
        return AgentState(
            query=query, selected_note_ids=[], retrieved_chunk_ids=[], tool_result_refs=[],
            planned_changes=[], approved_changes=[], step_count=0, retrieval_step_count=0,
            error_count=0, consecutive_error_count=0, same_tool_call_count=0,
            no_progress_count=0, last_tool_call_hash="", last_error_signature="",
            pending_tool="", pending_args_ref="", final_answer="", stop_reason="",
        )

    def run(self, query: str, thread_id: str) -> dict[str, Any]:
        if not query.strip():
            return {"final_answer": FALLBACK + "Empty query"}
        if self.graph.get_state({"configurable": {"thread_id": thread_id}}).values:
            raise ConfigError(f"Workflow {thread_id} already exists; resume the pending workflow")
        return self.graph.invoke(self._initial(query),
                                 {"configurable": {"thread_id": thread_id}, "recursion_limit": 64})

    def resume(self, thread_id: str, *, approved: bool) -> dict[str, Any]:
        return self.graph.invoke(Command(resume={"approved": approved}),
                                 {"configurable": {"thread_id": thread_id}, "recursion_limit": 64})

    def _route(self, state: AgentState) -> dict:
        return {"intent": route_intent(state["query"])}

    def _direct_search(self, state: AgentState) -> dict:
        search_query = re.sub(r"^(?:search|find|搜索|查找|检索)\s*[:： ]\s*", "",
                              state["query"], count=1, flags=re.IGNORECASE).strip()
        if not search_query:
            return {"final_answer": FALLBACK + "Empty search query"}
        try:
            ref, notes, chunks, summary = self.tools.read("search_notes", {"query": search_query})
            return {"tool_result_refs": [ref], "selected_note_ids": notes,
                    "retrieved_chunk_ids": chunks, "final_answer": summary,
                    "step_count": 1, "retrieval_step_count": 1}
        except Exception as exc:
            return {"final_answer": FALLBACK + f"Search failed: {type(exc).__name__}: {exc}"}

    def _rag(self, state: AgentState) -> dict:
        if self.answer_question is None:
            return {"final_answer": FALLBACK + "Answer service is unavailable"}
        try:
            answer, note_ids, chunk_ids = self.answer_question(state["query"])
            return {"final_answer": answer, "selected_note_ids": note_ids,
                    "retrieved_chunk_ids": chunk_ids, "step_count": 1, "retrieval_step_count": 1}
        except Exception as exc:
            return {"final_answer": FALLBACK + f"Answer failed: {type(exc).__name__}: {exc}"}

    def _summaries(self, state: AgentState) -> list[str]:
        # References are resolved into bounded summaries; the entire state is never a prompt.
        summaries = []
        for ref in state.get("tool_result_refs", [])[-3:]:
            item = self.artifacts.get(ref)
            if isinstance(item, dict) and "summary" in item:
                summaries.append(str(item["summary"])[:600])
        return summaries

    def _decide(self, state: AgentState) -> dict:
        check_shutdown()
        metric("agent.state", steps=state["step_count"],
               retrieval_steps=state["retrieval_step_count"], errors=state["error_count"])
        if state.get("final_answer"):
            return {}
        if state["step_count"] >= self.limits.max_steps:
            return self._stop("Maximum steps reached")
        if state["consecutive_error_count"] >= self.limits.max_consecutive_errors:
            return self._stop("Repeated invalid tool calls")
        if state["no_progress_count"] >= self.limits.max_no_progress_steps:
            return self._stop("No progress")
        try:
            with measure("agent.planner"):
                decision = self.planner.decide(
                    query=state["query"], intent=state["intent"],
                    recent_summaries=self._summaries(state), available_tools=tuple(sorted(ALL_TOOLS)),
                )
        except Exception as exc:
            return self._stop(f"Planner failed: {type(exc).__name__}: {exc}")
        if decision.final_answer is not None:
            if state["intent"] in ("read_operation", "organization_task") and not state["tool_result_refs"]:
                return self._stop("No evidence was read")
            return {"final_answer": decision.final_answer}
        if decision.tool not in ALL_TOOLS:
            return self._failure(state, f"Invalid tool: {decision.tool}", "invalid")
        if decision.tool in WRITE_TOOLS and state["intent"] not in ("write_operation", "organization_task"):
            return self._stop("Write tool is not authorized by the user's request")
        if decision.tool == "search_notes" and state["retrieval_step_count"] >= self.limits.max_retrieval_steps:
            return self._stop("Maximum retrieval steps reached")
        signature = hashlib.sha256(json.dumps(
            [decision.tool, decision.args], sort_keys=True, ensure_ascii=False,
        ).encode()).hexdigest()
        same = state["same_tool_call_count"] + 1 if signature == state["last_tool_call_hash"] else 1
        if same > self.limits.max_same_tool_call:
            return self._stop("Repeated identical tool call")
        args_ref = self.artifacts.put(decision.args)
        return {"pending_tool": decision.tool, "pending_args_ref": args_ref,
                "last_tool_call_hash": signature, "same_tool_call_count": same}

    @staticmethod
    def _after_decision(state: AgentState) -> str:
        if state.get("final_answer"):
            return END
        tool = state.get("pending_tool", "")
        if tool in READ_TOOLS:
            return "execute_read"
        if tool in WRITE_TOOLS:
            return "plan_write"
        return "decide"

    @staticmethod
    def _stop(reason: str) -> dict:
        return {"stop_reason": reason, "final_answer": FALLBACK + reason, "pending_tool": ""}

    def _failure(self, state: AgentState, message: str, signature: str) -> dict:
        metric("agent.tool_error", errors=state["error_count"] + 1,
               consecutive_errors=state["consecutive_error_count"] + 1)
        repeat = (signature == state.get("last_error_signature") and
                  state.get("same_tool_call_count", 0) >= self.limits.max_same_tool_call)
        if repeat:
            return {**self._stop("Repeated invalid tool calls"),
                    "error_count": state["error_count"] + 1}
        count = state["consecutive_error_count"] + 1
        if count >= self.limits.max_consecutive_errors:
            return {**self._stop("Repeated invalid tool calls"),
                    "error_count": state["error_count"] + 1,
                    "consecutive_error_count": count}
        ref = self.artifacts.put({"summary": message[:600]})
        return {"tool_result_refs": [*state["tool_result_refs"], ref],
                "error_count": state["error_count"] + 1,
                "consecutive_error_count": count,
                "no_progress_count": state["no_progress_count"] + 1,
                "last_error_signature": signature, "pending_tool": ""}

    def _execute_read(self, state: AgentState) -> dict:
        check_shutdown()
        try:
            args = self.artifacts.get(state["pending_args_ref"])
            ref, notes, chunks, summary = self.tools.read(state["pending_tool"], args)
            summary_ref = self.artifacts.put({"summary": summary, "result_ref": ref})
            return {"tool_result_refs": [*state["tool_result_refs"], summary_ref],
                    "selected_note_ids": list(dict.fromkeys([*state["selected_note_ids"], *notes])),
                    "retrieved_chunk_ids": list(dict.fromkeys([*state["retrieved_chunk_ids"], *chunks])),
                    "step_count": state["step_count"] + 1,
                    "retrieval_step_count": state["retrieval_step_count"] + (state["pending_tool"] == "search_notes"),
                    "consecutive_error_count": 0, "no_progress_count": 0,
                    "last_error_signature": "", "pending_tool": ""}
        except Exception as exc:
            return {**self._failure(state, f"{type(exc).__name__}: {exc}",
                                    f"{state['last_tool_call_hash']}:{type(exc).__name__}:{exc}"),
                    "step_count": state["step_count"] + 1,
                    "retrieval_step_count": state["retrieval_step_count"] + (state["pending_tool"] == "search_notes")}

    def _plan_write(self, state: AgentState) -> dict:
        check_shutdown()
        try:
            args = self.artifacts.get(state["pending_args_ref"])
            ref, preview = self.tools.plan_write(state["pending_tool"], args)
            preview_ref = self.artifacts.put({"summary": f"Proposed {state['pending_tool']}",
                                              "preview": preview})
            return {"planned_changes": [*state["planned_changes"], ref],
                    "tool_result_refs": [*state["tool_result_refs"], preview_ref],
                    "step_count": state["step_count"] + 1,
                    "consecutive_error_count": 0, "no_progress_count": 0,
                    "pending_tool": ""}
        except Exception as exc:
            return {**self._failure(state, f"{type(exc).__name__}: {exc}",
                                    f"{state['last_tool_call_hash']}:{type(exc).__name__}:{exc}"),
                    "step_count": state["step_count"] + 1}

    def _approve(self, state: AgentState) -> dict:
        plan_ref = state["planned_changes"][-1]
        response = interrupt({"kind": "write_approval", "plan_ref": plan_ref,
                              "preview_ref": state["tool_result_refs"][-1]})
        if not isinstance(response, dict) or response.get("approved") is not True:
            return {"approved_changes": [], "final_answer": "Write cancelled by user."}
        return {"approved_changes": [*state["approved_changes"], plan_ref]}

    def _apply_write(self, state: AgentState) -> dict:
        check_shutdown()
        try:
            result = self.tools.apply_write(state["approved_changes"][-1])
            message = "Change applied."
            if result.index_dirty:
                message += f" Index update failed: {result.index_error}"
            return {"final_answer": message}
        except Exception as exc:
            return self._stop(f"Write failed: {type(exc).__name__}: {exc}")
