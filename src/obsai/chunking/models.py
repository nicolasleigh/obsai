"""Chunk output and sizing policy.

该模块定义了知识库文本切片（Chunking）的核心数据结构与窗口配置策略。

核心设计思想：
1. 三级 Token 窗口策略（ChunkingOptions）：通过 min / target / max 阶梯阈值，
   在保持 Markdown 段落和标题原子性完整的同时，有效避免语义碎片化与过长切片。
2. 上下文富集（Context Enrichment）：Chunk 模型区分原始文本（raw_content）与向量
   文本（embedding_text），将标题与大纲面包屑嵌入向量表示中，消除语义孤岛。
3. 双哈希增量感知（Dual Hashing）：分别计算正文与向量文本的 SHA-256 哈希，
   精确识别仅修改标题/大纲或仅修改正文的场景，最大化节省 Embedding API 调用开销。
"""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True, slots=True)
class ChunkingOptions:
    """切片大小策略与 Token 预算配置（不可变轻量值对象）。"""

    min_tokens: int = 80  # 最小切片阈值（过小段落将尽可能向后合并，避免语义碎片化）
    target_tokens: int = 260  # 目标切片大小基准线（切片算法以此为中心进行段落聚合）
    max_tokens: int = 400  # 单个切片硬性上限（超出将强制分块或停止接纳新段落）

    def __post_init__(self) -> None:
        """防御性校验：确保切片窗口参数满足 0 < min <= target <= max 的逻辑递增约束。"""
        if not 0 < self.min_tokens <= self.target_tokens <= self.max_tokens:
            raise ValueError("Require 0 < min_tokens <= target_tokens <= max_tokens")


class Chunk(BaseModel):
    """知识库分块实体模型，全文检索（BM25）、向量检索与 RAG 证据引用的基本单位。"""

    # 严禁传入额外未声明字段，且实例创建后只读不可变，防止流水线传递中被意外篡改
    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str  # 切片全局唯一标识符
    note_id: str  # 所属原始笔记的唯一标识（关联源文件）
    heading_path: list[str]  # 标题面包屑层级路径（如 ["系统设计", "存储层", "SQLite"]）
    block_id: str | None = None  # Obsidian 原生块引用 ID（如 ^block-123，支持精准跳转），默认为 None
    raw_content: str  # 原始 Markdown 切片文本（用于原样展示与命中高亮）
    embedding_text: str  # 携带标题与大纲路径的富文本（专门用于送入向量模型生成高质量 Embedding）
    token_count: int  # 该切片估算的 Token/字符单位数量
    content_hash: str  # raw_content 的 SHA-256 哈希值，用于快速检测正文变动
    embedding_text_hash: str  # embedding_text 的 SHA-256 哈希值，用于增量判定是否需重新计算向量
    position: int  # 该切片在整篇笔记中的自然顺序序号（0-indexed），用于还原上下文连续性
    metadata: dict[str, Any] = Field(default_factory=dict)  # 可扩展的附加元数据字典
