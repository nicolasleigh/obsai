"""Embedding and vector storage contracts.

该模块定义了系统向量嵌入流水线（Embedding Pipeline）与向量数据库存储（Vector Storage）
的核心领域契约模型、数据类与协议接口（Protocol）。

核心设计哲学：
1. 向量空间强隔离（Vector Space Isolation）：
   在 RAG 系统中，严禁混用不同模型或不同维度的向量。通过不可变的 EmbeddingGeneration
   定义代际指纹（id），确保底层向量表按代际隔离，杜绝向量空间污染与维度不匹配错误。
2. 复合缓存指纹机制（Composite Cache Key）：
   调用大模型 Embedding API 具有时间与经济成本。EmbeddingGeneration.cache_key()
   将模型代际四元组与文本哈希组合，实现了文本变更或模型升级时的自动安全失效与命中复用。
3. 依赖反转与协议解耦（Protocol-based Abstraction）：
   采用 typing.Protocol 定义 EmbeddingProvider 和 VectorStore，
   使业务核心仅依赖抽象接口，轻松适配 OpenAI、本地模型以及各类向量存储引擎（如 SQLite-vec），
   大幅提高系统的可扩展性与单测便利度。
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from obsai.retrieval.models import SearchFilters, SearchResult


@dataclass(frozen=True)
class EmbeddingGeneration:
    """向量模型代际标识不可变数据类。

    唯一表征一个向量模型配置的“基因身份证”。一旦实例化便完全冻结（frozen=True），
    保证在并发检索、批处理嵌入与数据库写入全流程中的线程安全与不可篡改性。
    """

    provider: str        # 向量模型提供商（如 "openai", "local"）
    model: str           # 模型名称（如 "text-embedding-3-small"）
    model_version: str   # 模型版本标识（如 "1"）
    dimensions: int      # 输出向量维度（如 1536, 3072）

    def __post_init__(self) -> None:
        """后初始化验证：防御性断言所有代际属性非空且维度为正整数。"""
        if not all((self.provider, self.model, self.model_version)) or self.dimensions < 1:
            raise ValueError("Embedding generation requires identity and positive dimensions")

    @property
    def id(self) -> str:
        """计算当前代际规格的全局唯一 SHA-256 指纹。

        用于底层向量存储（如 sqlite-vec）分区或物理表标识，隔离不同模型的向量空间。
        """
        return hashlib.sha256(
            json.dumps(
                [self.provider, self.model, self.model_version, self.dimensions],
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()

    def cache_key(self, embedding_text_hash: str) -> str:
        """根据切片文本哈希与当前模型代际规格，计算唯一的切片向量缓存键。

        只要模型或文本内容发生变化，缓存键即改变，确保向量缓存的强一致性。

        :param embedding_text_hash: 归一化后待嵌入文本的 SHA-256 哈希值
        :return: 64 位 SHA-256 缓存键字符串
        """
        return hashlib.sha256(
            json.dumps(
                [self.provider, self.model, self.model_version, self.dimensions,
                 embedding_text_hash],
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()


class EmbeddingProvider(Protocol):
    """向量生成服务提供商协议接口。

    规范了所有外部/本地向量提供商（如 OpenAIEmbeddingProvider）必须遵循的结构契约。
    """

    generation: EmbeddingGeneration  # 该提供商绑定的向量代际规范

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """异步批量将文本列表转换为对应的高维稠密向量。

        :param texts: 待向量化的纯文本列表
        :return: 顺序一一对应的浮点数向量列表
        """
        ...


class VectorStore(Protocol):
    """向量数据库存储与相似度检索协议接口。

    规范了向量持久化层（如基于 sqlite-vec 的 SQLite 存储引擎）的增删改查操作契约。
    """

    def upsert(
        self,
        generation: EmbeddingGeneration,
        chunk_id: str,
        embedding_text_hash: str,
        vector: list[float],
        token_count: int,
    ) -> None:
        """插入或更新单个切片的向量及元数据。

        :param generation: 目标向量代际规范（确保写入正确分区）
        :param chunk_id: 知识切片唯一 ID
        :param embedding_text_hash: 文本内容哈希（用于检测内容是否变更）
        :param vector: 稠密浮点向量数据
        :param token_count: 切片消耗的 Token 计数
        """
        ...

    def delete(self, generation: EmbeddingGeneration, chunk_id: str) -> None:
        """从指定代际空间中物理删除某个切片的向量。

        :param generation: 目标向量代际规范
        :param chunk_id: 待删除的切片唯一 ID
        """
        ...

    def search(
        self,
        generation: EmbeddingGeneration,
        vector: list[float],
        limit: int = 10,
        filters: SearchFilters | None = None,
    ) -> list[SearchResult]:
        """在指定代际空间内执行向量相似度（KNN / ANN）近似搜索。

        :param generation: 目标向量代际规范（仅在相同空间内计算相似度）
        :param vector: 查询问题的向量（Query Vector）
        :param limit: 最多返回的检索切片数量，默认 10
        :param filters: 可选的元数据过滤条件（如笔记路径、标签过滤等）
        :return: 包含相似度得分与切片元数据的 SearchResult 列表
        """
        ...
