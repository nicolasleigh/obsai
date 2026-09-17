"""Small checkpoint state: IDs and references, never note bodies or file snapshots.

该模块定义了 LangGraph Agent 工作流的状态数据结构。
遵循“仅存引用（Reference-only）”的设计原则：State 中仅保存轻量级的 ID、哈希值、
计数器和产物引用（Artifact References），绝不存储大篇幅的笔记正文或文件快照，
以确保 LangGraph 检查点（Checkpointer）的高效持久化与轻量序列化。
"""

from typing import Literal, TypedDict


# 用户意图分类类型
Intent = Literal[
    "direct_search",      # 直接搜索：单步执行关键词/向量检索后快速返回
    "rag_question",       # RAG 问答：结合证据引用的高质量知识问答
    "read_operation",     # 只读操作：查看单篇笔记、反向链接、出链等图结构查询
    "write_operation",    # 写入操作：新建、编辑、移动、废弃笔记等需人机审核的操作
    "organization_task",  # 整理任务：多步知识库归档、结构重组综合任务
]


class AgentState(TypedDict, total=False):
    """LangGraph 状态机在各节点间传递的全局上下文状态字典。"""

    # --- 输入与意图 ---
    query: str  # 用户原始输入的提问或任务指令
    intent: Intent  # 入口路由器识别出的用户意图类别

    # --- 检索与知识引用（轻量 ID 引用） ---
    selected_note_ids: list[str]  # 当前工作流涉及或命中的笔记 ID 列表
    retrieved_chunk_ids: list[str]  # 检索阶段命中的文本分块（Chunk）ID 列表
    tool_result_refs: list[str]  # 工具执行结果存入 ArtifactStore 后的产物引用 key 列表

    # --- 写操作提议与人工审核（HITL） ---
    planned_changes: list[str]  # 模型规划生成的写操作变更提议引用（Proposal Refs）
    approved_changes: list[str]  # 经过人类审查并确认批准的变更引用列表

    # --- 有界执行与安全熔断计数器 ---
    step_count: int  # Agent 当前已执行的总步数（受 AgentLimits.max_steps 限制）
    retrieval_step_count: int  # 检索类工具的调用次数（受 AgentLimits.max_retrieval_steps 限制）
    error_count: int  # 执行过程中累计发生的错误总次数
    consecutive_error_count: int  # 连续报错次数（用于快速熔断，防止反复重试失效工具）
    same_tool_call_count: int  # 连续调用相同工具及参数的次数（防止陷入局部死循环）
    no_progress_count: int  # 连续未产生新信息/进展的步数

    # --- 循环检测与错误特征 ---
    last_tool_call_hash: str  # 上一次工具调用（名称 + 排序后参数）的 SHA-256 哈希值
    last_error_signature: str  # 上一次报错的特征字符串（用于比对是否连续发生同类错误）

    # --- 待执行/待审批工具暂存 ---
    pending_tool: str  # 当前规划出但尚未执行的工具名称（等待 HITL 审批或执行节点拉取）
    pending_args_ref: str  # 待执行工具参数存入 ArtifactStore 后的产物引用 key

    # --- 输出与终态 ---
    final_answer: str  # Agent 生成的最终回复文本或终止时的错误提示
    stop_reason: str  # 工作流终止的原因标识（如 completed、max_steps、error_abort 等）
