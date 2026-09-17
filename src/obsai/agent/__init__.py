"""Bounded LangGraph agent workflow.

该模块为 obsai.agent 子包的公共 API 导出入口。
将内部的状态管理（state）、工具集（tools）、路由（router）及规划器（openai_planner）进行封装，
对外统一暴露有界 Agent 工作流及其核心配置与决策协议。
"""

from obsai.agent.workflow import (
    AgentLimits,
    AgentWorkflow,
    DecisionProvider,
    ToolDecision,
)

__all__ = [
    "AgentLimits",       # Agent 执行安全边界配置（最大步数、连续错误与工具重复调用上限等）
    "AgentWorkflow",     # 基于 LangGraph 的有界 Agent 状态图工作流
    "DecisionProvider",  # 决策规划器抽象协议（Protocol，定义 decide 决策签名）
    "ToolDecision",      # 规划器决策结果数据结构（包含工具调用参数或最终答案）
]
