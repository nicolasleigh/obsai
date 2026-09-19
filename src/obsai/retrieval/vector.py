"""向量语义检索器模块（Semantic retriever using the same SearchResult contract as FTS）。

核心设计哲学与安全架构：
1. 统一输出契约（Unified SearchResult Contract）：
   与基于 SQLite FTS5 的关键词检索器（FTSRetriever）保持完全对等的抽象契约，
   输出结构完全一致的强类型 `SearchResult` 实体列表，为上层混合检索（HybridRetriever）与知识图谱扩展（GraphRetriever）
   提供无缝插拔与平滑融合的基础。
2. 计费授权门禁（Budget & Consent Protection）：
   实时将搜索问句转化为稠密向量需要调用云端嵌入模型 API（如 OpenAI text-embedding-3-small），
   通过显式的 `approved` 授权标识进行门禁拦截，杜绝未经用户确认的潜在资金开销。
3. 模型世代防呆对齐（Generation Safety & Dimension Alignment）：
   执行向量检索时显式绑定当前流水线的世代信息（`self.pipeline.generation`），
   确保查询向量与库中存储向量的嵌入模型、维度（Dimensions）完全一致，杜绝跨模型向量交叉比对引发的数学异常。
4. 异步同步衔接与性能度量（Async Bridging & Telemetry）：
   在标准的同步 `search` 方法中通过 `asyncio.run` 驱动异步网络嵌入请求，
   并利用 `@measured("retrieval.vector")` 收集完整的端到端检索耗时。
"""

import asyncio

from obsai.embedding.pipeline import EmbeddingPipeline
from obsai.retrieval.models import SearchFilters, SearchResult
from obsai.storage.vectors import SQLiteVectorStore
from obsai.telemetry import measured


class VectorRetriever:
    """基于向量嵌入与近邻匹配的语义检索器，实现标准 Retriever 协议契约。"""

    def __init__(
        self, store: SQLiteVectorStore, pipeline: EmbeddingPipeline, *, approved: bool = False
    ):
        """初始化向量语义检索器。

        :param store: 底层 SQLite 向量数据库仓储实例
        :param pipeline: 嵌入流水线管理器（负责文本嵌入模型调用与版本世代管理）
        :param approved: 计费授权确认标志（True 表示用户已批准模型 API 调用产生 Token 费用）
        """
        self.store = store
        self.pipeline = pipeline
        self.approved = approved

    @measured("retrieval.vector")
    def search(
        self,
        query: str,
        limit: int = 10,
        filters: SearchFilters | None = None,
    ) -> list[SearchResult]:
        """执行自然语言向量语义检索。

        执行流程：
        1. 快速短路：若问句为空白或 limit <= 0，立即返回空列表，防止产生无意义的 API Token 消耗；
        2. 问句嵌入：调用 pipeline.embed_query 将搜索文本转换为稠密浮点向量（校验 approved 授权）；
        3. 向量近邻搜索：在向量库中基于当前模型世代（generation）执行余弦近邻检索与元数据过滤。

        :param query: 自然语言搜索问句或主题词
        :param limit: 最大返回切片结果数量（默认 10）
        :param filters: 可选的高级元数据过滤规则
        :return: 符合统一契约的 SearchResult 列表（source='semantic'）
        """
        # 1. 快速短路剪枝：避免对空文本发起外部模型网络请求，防止浪费 Token
        if not query.strip() or limit <= 0:
            return []

        # 2. 异步转同步：调用嵌入流水线生成搜索问句的嵌入向量
        vector = asyncio.run(self.pipeline.embed_query(query, approved=self.approved))

        # 3. 在底层向量库中执行近邻搜索，绑定世代 ID 并内联应用元数据过滤
        return self.store.search(self.pipeline.generation, vector, limit, filters)

