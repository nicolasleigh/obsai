"""检索重排序器扩展边界与保底实现模块（Optional reranking boundary; V1 keeps fused order）。

核心设计哲学：
1. 两阶段检索架构解耦（Two-Stage Retrieval Architecture Decoupling）：
   在混合多路初筛召回（FTS5 关键词 + Vector 向量嵌入）与最终结果交付之间建立清晰的标准抽象接口，
   为未来无缝集成 Cross-Encoder 深度学习模型（如 BGE-Reranker、ColBERT）或大模型 LLM 重排提供标准插件化边界。
2. 零开销默认直通保底（Zero-Overhead Identity Baseline）：
   内置 `NoOpReranker` 作为默认保底实现，直接复用上游 RRF 倒数排序融合输出的排序并执行简单切片截断，
   免除模型推理与网络往返开销，保障本地知识库检索的极致响应速度。
"""

from typing import Protocol

from obsai.retrieval.models import SearchResult


class Reranker(Protocol):
    """检索重排序器抽象协议（Python Structural Protocol）。

    定义两阶段检索中第二阶段精细打分与重排的通用标准契约。
    任何实现了符合该签名的 rerank 方法的类均自动满足此协议。
    """

    def rerank(
        self, query: str, results: list[SearchResult], limit: int
    ) -> list[SearchResult]:
        """对初筛候选切片列表结合原始搜索问句进行二次精细重排序。

        :param query: 用户输入的原始搜索问句（供交叉注意力模型或相关度判定使用）
        :param results: 上游粗召回或 RRF 融合阶段输出的候选切片列表
        :param limit: 最终期望截取返回的结果切片数量上限
        :return: 经过精细重排并截断后的 SearchResult 列表
        """
        ...


class NoOpReranker:
    """默认直通/空操作重排序器（No-Operation Reranker）。

    ObsAI 的默认重排器实现。不改变上游输入的相对顺序，直接按 limit 截取前部切片，
    100% 保留 RRF 融合算法的确定性排序结果，零额外计算开销。
    """

    def rerank(
        self, query: str, results: list[SearchResult], limit: int
    ) -> list[SearchResult]:
        """直通返回前 limit 条候选切片，保持原有排序。

        :param query: 搜索问句（本实现忽略）
        :param results: 候选切片列表
        :param limit: 截断返回数量上限
        :return: 截断后的 SearchResult 列表
        """
        return results[:limit]
