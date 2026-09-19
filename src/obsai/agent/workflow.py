"""Bounded LangGraph workflow with reference-only state and explicit write HITL.

该模块是 Agent 的核心工作流调度引擎，基于 LangGraph 状态机实现。

核心架构与设计原则：
1. 有界执行（Bounded Execution）：通过 AgentLimits 严格限制总步数、检索步数、连续报错
   及相同工具重复调用上限，配备无进展安全熔断，杜绝死循环与 Token 异常消耗。
2. 仅存引用状态（Reference-only State）：AgentState 仅跟踪轻量 ID、哈希与产物引用，
   大体量数据（搜索结果、笔记全文、事务计划）存入 ArtifactStore，保证 Checkpoint 高性能。
3. 显式写操作人机协同（Explicit Write HITL）：写操作必须经过 plan -> diff preview -> interrupt 挂起
   -> 人工审批确认（resume）-> 事务原子提交与全库反向链接更新。
4. 确定性快速路径（Fast Paths）：直接搜索（direct_search）与常规问答（rag）通过入口路由器快速直出，
   无需启动多轮决策循环。
"""

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

# 异常终止/安全熔断时的通用回退前缀
FALLBACK = "Agent could not safely continue.\n\nReason: "


@dataclass(frozen=True)
class AgentLimits:
    """Agent 有界执行安全限制配置（不可变值对象）。"""

    max_steps: int = 15  # 工作流允许执行的总步数硬上限
    max_retrieval_steps: int = 5  # 检索类工具（search_notes）最大调用次数上限
    max_consecutive_errors: int = 3  # 连续报错熔断阈值（工具连续报错达到此数值即终止）
    max_same_tool_call: int = 2  # 相同参数重复调用同一工具上限（防止局部死循环）
    max_no_progress_steps: int = 3  # 连续未产生有效新信息的最大容忍步数


@dataclass(frozen=True)
class ToolDecision:
    """决策规划器（Planner）输出的标准决策数据结构。"""

    tool: str | None = None  # 下一步调用的工具名称（如 read_note, create_note 等）
    args: dict[str, Any] = field(default_factory=dict)  # 工具调用的参数字典
    final_answer: str | None = None  # 若任务已完成，给出的最终文本回答


class DecisionProvider(Protocol):
    """决策规划器抽象协议（Protocol），定义 LLM 规划器的标准接口。"""

    def decide(
        self,
        *,
        query: str,
        intent: str,
        recent_summaries: list[str],
        available_tools: tuple[str, ...],
    ) -> ToolDecision:
        """根据用户输入、意图、近期工具摘要和可用工具生成决策。"""
        ...


class AgentWorkflow:
    """基于 LangGraph 的有界 Agent 状态机工作流。"""

    def __init__(
        self,
        tools: AgentTools,
        artifacts: ArtifactStore,
        planner: DecisionProvider,
        *,
        checkpointer: Any,
        answer_question: Callable[[str], tuple[str, list[str], list[str]]] | None = None,
        limits: AgentLimits = AgentLimits(),
    ):
        """初始化工作流并构建 LangGraph 状态图拓扑。

        :param tools: 工具执行边界层实例
        :param artifacts: 产物持久化存储库实例
        :param planner: 决策规划器实例（实现 DecisionProvider 协议）
        :param checkpointer: LangGraph 状态持久化检查点存储（如 SqliteSaver / InMemorySaver）
        :param answer_question: 预构建的 RAG 问答服务（快速问答通道）
        :param limits: 安全边界限制配置
        """
        self.tools = tools
        self.artifacts = artifacts
        self.planner = planner
        self.answer_question = answer_question
        self.limits = limits

        # 1. 创建基于 AgentState 的状态图
        graph = StateGraph(AgentState)

        # 2. 注册图节点（Node）
        graph.add_node("route", self._route)                    # 入口意图路由节点
        graph.add_node("direct_search", self._direct_search)    # 快速单步搜索节点
        graph.add_node("rag", self._rag)                        # 快速 RAG 问答节点
        graph.add_node("decide", self._decide)                  # LLM 决策规划节点
        graph.add_node("execute_read", self._execute_read)      # 只读工具执行节点
        graph.add_node("plan_write", self._plan_write)          # 写操作提案生成节点
        graph.add_node("approve", self._approve)                # 人机协同审查挂起节点（HITL）
        graph.add_node("apply_write", self._apply_write)        # 写操作事务原子落盘节点

        # 3. 编排工作流边与条件转移逻辑（Edges & Conditional Edges）
        graph.add_edge(START, "route")

        # 路由分流：直接搜索走 direct_search，常规提问走 rag，复杂任务进入 decide 规划循环
        graph.add_conditional_edges(
            "route",
            lambda state: (
                "direct_search" if state["intent"] == "direct_search"
                else "rag" if state["intent"] == "rag_question"
                else "decide"
            ),
        )
        graph.add_edge("direct_search", END)  # 单步搜索完成后直接结束
        graph.add_edge("rag", END)            # 单步 RAG 回答完成后直接结束

        # 规划后跳转：根据决策结果流向 execute_read、plan_write、再次 decide 或 END
        graph.add_conditional_edges("decide", self._after_decision)

        # 读操作执行后，将结果摘要带回 decide 节点继续规划
        graph.add_edge("execute_read", "decide")

        # 写提案生成后，若有待批提案则流向 approve 等待人类审核，否则返回 decide
        graph.add_conditional_edges(
            "plan_write",
            lambda state: "approve" if state.get("planned_changes") else "decide",
        )

        # 人机审查：用户批准则流向 apply_write 执行写入，否则流向 END 终止
        graph.add_conditional_edges(
            "approve",
            lambda state: "apply_write" if state.get("approved_changes") else END,
        )

        # 写操作原子应用完成后结束
        graph.add_edge("apply_write", END)

        # 4. 编译状态图并绑定持久化检查点
        self.graph = graph.compile(checkpointer=checkpointer)

    @staticmethod
    def _initial(query: str) -> AgentState:
        """生成新会话初始化的空白 AgentState。

        遵循 Reference-only 原则，将所有计数器清零，所有引用列表置空。
        """
        return AgentState(
            query=query,
            selected_note_ids=[],
            retrieved_chunk_ids=[],
            tool_result_refs=[],
            planned_changes=[],
            approved_changes=[],
            step_count=0,
            retrieval_step_count=0,
            error_count=0,
            consecutive_error_count=0,
            same_tool_call_count=0,
            no_progress_count=0,
            last_tool_call_hash="",
            last_error_signature="",
            pending_tool="",
            pending_args_ref="",
            final_answer="",
            stop_reason="",
        )

    def run(self, query: str, thread_id: str) -> dict[str, Any]:
        """启动一个全新的 Agent 工作流任务。

        :param query: 用户输入的原始提示词或任务指令
        :param thread_id: 会话线程唯一标识（绑定 LangGraph Checkpoint）
        :return: 工作流最终执行结果状态字典
        :raises ConfigError: 若该 thread_id 已存在活跃检查点，则禁止覆盖，提示调用 resume
        """
        if not query.strip():
            return {"final_answer": FALLBACK + "Empty query"}
        # 校验防重：如果该线程 ID 已有检查点状态，必须通过 resume 恢复，避免意外覆盖历史
        if self.graph.get_state({"configurable": {"thread_id": thread_id}}).values:
            raise ConfigError(f"Workflow {thread_id} already exists; resume the pending workflow")
        return self.graph.invoke(
            self._initial(query),
            {"configurable": {"thread_id": thread_id}, "recursion_limit": 64},
        )

    def resume(self, thread_id: str, *, approved: bool) -> dict[str, Any]:
        """恢复处于人机协同（HITL）中断挂起状态的工作流。

        :param thread_id: 处于 interrupt 挂起状态的会话线程 ID
        :param approved: 用户对写操作提案的审批决策（True=批准执行，False=撤回取消）
        :return: 恢复执行后的最终状态字典
        """
        return self.graph.invoke(
            Command(resume={"approved": approved}),
            {"configurable": {"thread_id": thread_id}, "recursion_limit": 64},
        )

    def _route(self, state: AgentState) -> dict:
        """入口意图路由节点：通过确定性正则规则识别用户意图。"""
        return {"intent": route_intent(state["query"])}

    def _direct_search(self, state: AgentState) -> dict:
        """快速直接搜索节点：剥离命令前缀，单步执行检索后快速退出（Fast Path）。"""
        # 剥离 "search:", "搜索：" 等前缀，提取纯检索关键词
        search_query = re.sub(
            r"^(?:search|find|搜索|查找|检索)\s*[:： ]\s*",
            "",
            state["query"],
            count=1,
            flags=re.IGNORECASE,
        ).strip()
        if not search_query:
            return {"final_answer": FALLBACK + "Empty search query"}
        try:
            # 调用只读工具执行搜索
            ref, notes, chunks, summary = self.tools.read("search_notes", {"query": search_query})
            # 将搜索结果摘要记录到 ArtifactStore
            summary_ref = self.artifacts.put({
                "tool": "search_notes",
                "summary": summary,
                "args": {"query": search_query},
                "result_ref": ref,
            })
            return {
                "tool_result_refs": [summary_ref],
                "selected_note_ids": notes,
                "retrieved_chunk_ids": chunks,
                "final_answer": summary,
                "step_count": 1,
                "retrieval_step_count": 1,
            }
        except Exception as exc:
            return {"final_answer": FALLBACK + f"Search failed: {type(exc).__name__}: {exc}"}

    def _rag(self, state: AgentState) -> dict:
        """快速 RAG 问答节点：调用预置问答流水线，生成带证据引用的回答并直接退出（Fast Path）。"""
        if self.answer_question is None:
            return {"final_answer": FALLBACK + "Answer service is unavailable"}
        try:
            # 执行混合检索、上下文组装及 LLM 证据引用问答
            answer, note_ids, chunk_ids = self.answer_question(state["query"])
            summary_ref = self.artifacts.put({
                "tool": "rag_answer",
                "summary": f"Generated answer using {len(note_ids)} notes",
                "args": {"query": state["query"]},
            })
            return {
                "final_answer": answer,
                "selected_note_ids": note_ids,
                "retrieved_chunk_ids": chunk_ids,
                "tool_result_refs": [summary_ref],
                "step_count": 1,
                "retrieval_step_count": 1,
            }
        except Exception as exc:
            return {"final_answer": FALLBACK + f"Answer failed: {type(exc).__name__}: {exc}"}

    def _summaries(self, state: AgentState) -> list[str]:
        """从产物库中解析并提取最近工具调用的简明摘要列表。

        关键约束：
        - 仅提取最近 3 次工具调用的摘要（[-3:]）。
        - 单条摘要截断至前 600 字符。
        - 绝不把完整 State 或笔记正文塞入 Prompt，从根源保证有界轻量。
        """
        summaries = []
        for ref in state.get("tool_result_refs", [])[-3:]:
            item = self.artifacts.get(ref)
            if isinstance(item, dict) and "summary" in item:
                summaries.append(str(item["summary"])[:600])
        return summaries

    def _decide(self, state: AgentState) -> dict:
        """核心决策规划节点：执行安全边界检查、防死循环哈希检测，并调度 LLM 规划下一步。"""
        # 1. 检查是否有系统优雅退出请求（SIGINT / SIGTERM）
        check_shutdown()

        # 2. 上报当前状态指标（总步数、检索步数、累计错误数）
        metric(
            "agent.state",
            steps=state["step_count"],
            retrieval_steps=state["retrieval_step_count"],
            errors=state["error_count"],
        )

        # 3. 若已有最终答案，无需继续规划
        if state.get("final_answer"):
            return {}

        # 4. 安全熔断规则检查（四大硬性防护）
        if state["step_count"] >= self.limits.max_steps:
            return self._stop("Maximum steps reached")  # 达到总执行步数上限
        if state["consecutive_error_count"] >= self.limits.max_consecutive_errors:
            return self._stop("Repeated invalid tool calls")  # 连续错误达到熔断阈值
        if state["no_progress_count"] >= self.limits.max_no_progress_steps:
            return self._stop("No progress")  # 连续无实质进展，主动停止

        # 5. 调用 Planner 做出规划决策（带性能耗时埋点）
        try:
            with measure("agent.planner"):
                decision = self.planner.decide(
                    query=state["query"],
                    intent=state["intent"],
                    recent_summaries=self._summaries(state),
                    available_tools=tuple(sorted(ALL_TOOLS)),
                )
        except Exception as exc:
            return self._stop(f"Planner failed: {type(exc).__name__}: {exc}")

        # 6. 分支 A：规划器决定给出最终回答（任务完成）
        if decision.final_answer is not None:
            # 防幻觉检查：若是只读或整理任务，但全流程未读取过任何笔记证据，禁止凭空臆造回答
            if state["intent"] in ("read_operation", "organization_task") and not state["tool_result_refs"]:
                return self._stop("No evidence was read")
            return {"final_answer": decision.final_answer}

        # 7. 分支 B：规划器决定调用工具，执行严格的安全防御校验
        # 校验 1：工具名必须属于全局白名单
        if decision.tool not in ALL_TOOLS:
            return self._failure(state, f"Invalid tool: {decision.tool}", "invalid")

        # 校验 2：写入权限约束（普通只读查询意图下严禁调用写工具，防止越权篡改）
        if decision.tool in WRITE_TOOLS and state["intent"] not in ("write_operation", "organization_task"):
            return self._stop("Write tool is not authorized by the user's request")

        # 校验 3：检索步数上限防护（防止检索工具调用过多消耗 Token）
        if decision.tool == "search_notes" and state["retrieval_step_count"] >= self.limits.max_retrieval_steps:
            return self._stop("Maximum retrieval steps reached")

        # 校验 4：循环死锁检测（计算当前工具名称与排序后参数的 SHA-256 特征哈希）
        signature = hashlib.sha256(
            json.dumps([decision.tool, decision.args], sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        same = state["same_tool_call_count"] + 1 if signature == state["last_tool_call_hash"] else 1
        if same > self.limits.max_same_tool_call:
            return self._stop("Repeated identical tool call")  # 重复调用相同参数工具超限，熔断

        # 8. 工具参数安全隔离：将完整参数对象存入 ArtifactStore，状态仅保留引用
        args_ref = self.artifacts.put(decision.args)
        return {
            "pending_tool": decision.tool,
            "pending_args_ref": args_ref,
            "last_tool_call_hash": signature,
            "same_tool_call_count": same,
        }

    @staticmethod
    def _after_decision(state: AgentState) -> str:
        """decide 节点后的条件路由函数：判断工作流的下一步走向。"""
        if state.get("final_answer"):
            return END  # 已生成最终回答，流向工作流结束
        tool = state.get("pending_tool", "")
        if tool in READ_TOOLS:
            return "execute_read"  # 待执行工具为只读工具，流向执行节点
        if tool in WRITE_TOOLS:
            return "plan_write"    # 待执行工具为写入工具，流向提案生成节点
        return "decide"            # 其他情况返回重新规划

    @staticmethod
    def _stop(reason: str) -> dict:
        """统一的熔断/终止辅助方法：设置终止原因并生成标准的兜底回答。"""
        return {"stop_reason": reason, "final_answer": FALLBACK + reason, "pending_tool": ""}

    def _failure(self, state: AgentState, message: str, signature: str) -> dict:
        """统一的工具执行或校验失败处理方法：累加错误计数并记录错误摘要。"""
        metric(
            "agent.tool_error",
            errors=state["error_count"] + 1,
            consecutive_errors=state["consecutive_error_count"] + 1,
        )

        # 检查是否为同特征错误的重复发生
        repeat = (
            signature == state.get("last_error_signature")
            and state.get("same_tool_call_count", 0) >= self.limits.max_same_tool_call
        )
        if repeat:
            return {**self._stop("Repeated invalid tool calls"), "error_count": state["error_count"] + 1}

        # 检查连续错误是否达到安全阈值
        count = state["consecutive_error_count"] + 1
        if count >= self.limits.max_consecutive_errors:
            return {
                **self._stop("Repeated invalid tool calls"),
                "error_count": state["error_count"] + 1,
                "consecutive_error_count": count,
            }

        # 将错误信息存入 ArtifactStore，供模型在后续步骤中看到失败原因并调整策略
        ref = self.artifacts.put({
            "tool": state.get("pending_tool") or "error",
            "summary": message[:600],
            "args": {},
        })
        return {
            "tool_result_refs": [*state["tool_result_refs"], ref],
            "error_count": state["error_count"] + 1,
            "consecutive_error_count": count,
            "no_progress_count": state["no_progress_count"] + 1,
            "last_error_signature": signature,
            "pending_tool": "",
        }

    def _execute_read(self, state: AgentState) -> dict:
        """执行只读工具节点：拉取参数、执行读取、沉淀产物并重置错误状态。"""
        check_shutdown()
        try:
            # 从 ArtifactStore 中还原工具参数对象
            args = self.artifacts.get(state["pending_args_ref"])
            ref, notes, chunks, summary = self.tools.read(state["pending_tool"], args)

            # 产物入库：将读取摘要与结果引用保存到 ArtifactStore
            summary_ref = self.artifacts.put({
                "tool": state["pending_tool"],
                "summary": summary,
                "args": args if isinstance(args, dict) else {},
                "result_ref": ref,
            })

            # 更新 AgentState：去重记录触达的笔记与切片，重置连续错误与无进展计数器
            return {
                "tool_result_refs": [*state["tool_result_refs"], summary_ref],
                "selected_note_ids": list(dict.fromkeys([*state["selected_note_ids"], *notes])),
                "retrieved_chunk_ids": list(dict.fromkeys([*state["retrieved_chunk_ids"], *chunks])),
                "step_count": state["step_count"] + 1,
                "retrieval_step_count": state["retrieval_step_count"] + (state["pending_tool"] == "search_notes"),
                "consecutive_error_count": 0,
                "no_progress_count": 0,
                "last_error_signature": "",
                "pending_tool": "",
            }
        except Exception as exc:
            # 捕获工具执行异常，调用统一失败处理
            return {
                **self._failure(
                    state,
                    f"{type(exc).__name__}: {exc}",
                    f"{state['last_tool_call_hash']}:{type(exc).__name__}:{exc}",
                ),
                "step_count": state["step_count"] + 1,
                "retrieval_step_count": state["retrieval_step_count"] + (state["pending_tool"] == "search_notes"),
            }

    def _plan_write(self, state: AgentState) -> dict:
        """生成写操作提案节点：仅计算文件变更计划与 Diff 预览，绝不接触磁盘。"""
        check_shutdown()
        try:
            args = self.artifacts.get(state["pending_args_ref"])
            ref, preview = self.tools.plan_write(state["pending_tool"], args)

            # 将终端 Diff 预览文本保存到产物库，供前端/CLI 展示
            preview_ref = self.artifacts.put({
                "tool": state["pending_tool"],
                "summary": f"Proposed {state['pending_tool']}",
                "args": args if isinstance(args, dict) else {},
                "preview": preview,
            })

            # 记录待审批计划引用（planned_changes），为后续 approve 节点提供输入
            return {
                "planned_changes": [*state["planned_changes"], ref],
                "tool_result_refs": [*state["tool_result_refs"], preview_ref],
                "step_count": state["step_count"] + 1,
                "consecutive_error_count": 0,
                "no_progress_count": 0,
                "pending_tool": "",
            }
        except Exception as exc:
            return {
                **self._failure(
                    state,
                    f"{type(exc).__name__}: {exc}",
                    f"{state['last_tool_call_hash']}:{type(exc).__name__}:{exc}",
                ),
                "step_count": state["step_count"] + 1,
            }

    def _approve(self, state: AgentState) -> dict:
        """人机协同审核节点（HITL）：触发 LangGraph 中断挂起，等待用户决策。"""
        plan_ref = state["planned_changes"][-1]

        # 调用 LangGraph 的 interrupt() 暂停状态机执行，并将审核请求载荷暴露给外部调用方
        response = interrupt({
            "kind": "write_approval",
            "plan_ref": plan_ref,
            "preview_ref": state["tool_result_refs"][-1],
        })

        # 外部调用 resume() 恢复工作流时，获取并校验用户的批准决策
        if not isinstance(response, dict) or response.get("approved") is not True:
            # 用户拒绝或撤销，清空批准列表并给出终止提示（流向 END）
            return {"approved_changes": [], "final_answer": "Write cancelled by user."}

        # 用户明确批准，将计划引用加入已批准清单（后续流向 apply_write）
        return {"approved_changes": [*state["approved_changes"], plan_ref]}

    def _apply_write(self, state: AgentState) -> dict:
        """写操作落盘节点：对已获人类批准的事务计划执行原子提交与全库反链重写。"""
        check_shutdown()
        try:
            result = self.tools.apply_write(state["approved_changes"][-1])
            message = "Change applied."
            if result.index_dirty:
                message += f" Index update failed: {result.index_error}"

            ref = self.artifacts.put({
                "tool": "apply_write",
                "summary": message,
                "args": {},
            })
            return {
                "final_answer": message,
                "tool_result_refs": [*state.get("tool_result_refs", []), ref],
            }
        except Exception as exc:
            return self._stop(f"Write failed: {type(exc).__name__}: {exc}")

