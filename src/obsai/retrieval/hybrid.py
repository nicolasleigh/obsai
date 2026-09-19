"""默认双路混合检索管线服务（Default retrieval pipeline with explicit semantic degradation reporting）。

核心设计哲学与高可用架构：
1. 双路召回与两阶段精排（Two-Stage Hybrid Retrieval Pipeline）：
   将 SQLite FTS5 关键词全文检索（高精确率）与向量嵌入语义检索（高泛化能力）并行触发；
   初筛阶段设置 `candidate_limit` 超额抓取候选切片池（Over-fetching），
   通过 RRF 算法进行多路无尺度依赖融合，最终交由 Reranker 重排序器精排输出。
2. 工业级语义降级熔断与高可用保障（Graceful Semantic Degradation）：
   当向量语义服务未配置、API Key 缺失、网络抖动、模型服务宕机或触发 Rate Limit 限流时，
   系统绝不崩溃中断，而是自动、静默、平滑地降级为纯关键字检索，
   并通过 `HybridOutcome` 显式向调用方暴露 `degraded=True` 与诊断警告，兼顾高可用性与可观察性。
3. 严格与宽容两种异常策略（Flexible Failure Policies）：
   - "warn"（默认模式）：容忍语义失败，降级运行并记录日志；
   - "strict"（严格模式）：任何语义失败均立刻抛出异常，满足强一致性测试或特定批处理需求。
"""

import logging
from dataclasses import dataclass
from typing import Literal

from obsai.errors import EmbeddingError
from obsai.retrieval.fusion import rrf_fuse
from obsai.retrieval.models import Retriever, SearchFilters, SearchResult
from obsai.retrieval.reranker import NoOpReranker, Reranker
from obsai.telemetry import measured

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HybridOutcome:
    """混合检索执行结果包装模型。

    封装检索产出的最终切片列表，以及检索过程中捕获的降级或非致命告警信息。
    """

    results: tuple[SearchResult, ...]
    """经过融合与重排后的最终切片结果元组。"""

    warnings: tuple[str, ...] = ()
    """检索执行过程中捕获的非致命警告或降级说明（例如语义后端不可用）。"""

    @property
    def degraded(self) -> bool:
        """判断当前检索是否处于降级运行状态（即存在任何非致命告警）。"""
        return bool(self.warnings)


class HybridRetriever:
    """默认双路混合检索器，实现标准 Retriever 协议，并提供状态感知检索能力。"""

    def __init__(
        self,
        keyword: Retriever,
        semantic: Retriever | None,
        *,
        candidate_limit: int = 20,
        rank_constant: int = 60,
        reranker: Reranker | None = None,
        on_semantic_failure: Literal["warn", "strict"] = "warn",
        semantic_unavailable_reason: str = "Semantic backend unavailable",
    ):
        """初始化混合检索器。

        :param keyword: 关键词全文检索器（必需，通常为 FTSRetriever）
        :param semantic: 向量语义检索器（可选，未配置嵌入模型或处于离线模式时为 None）
        :param candidate_limit: 每路单源初筛候选池容量（默认 20 条，确保 RRF 融合时具备充足的候选深度）
        :param rank_constant: RRF 平滑常数 k（默认 60）
        :param reranker: 精细重排序器（缺省时使用 NoOpReranker 空操作）
        :param on_semantic_failure: 语义服务异常处理策略（'warn' 记录告警并降级；'strict' 抛出异常）
        :param semantic_unavailable_reason: 语义检索器为 None 时的提示文案
        :raises ValueError: 当参数配置不合法时抛出
        """
        if candidate_limit < 1 or rank_constant < 1:
            raise ValueError("Hybrid candidate limit and RRF constant must be positive")
        if on_semantic_failure not in ("warn", "strict"):
            raise ValueError("Unknown semantic failure policy")
        self.keyword = keyword
        self.semantic = semantic
        self.candidate_limit = candidate_limit
        self.rank_constant = rank_constant
        self.reranker = reranker or NoOpReranker()
        self.on_semantic_failure = on_semantic_failure
        self.semantic_unavailable_reason = semantic_unavailable_reason
        # 缓存最近一次执行捕获的告警列表
        self.last_warnings: tuple[str, ...] = ()

    @measured("retrieval.hybrid")
    def search_with_status(
        self,
        query: str,
        limit: int = 10,
        filters: SearchFilters | None = None,
    ) -> HybridOutcome:
        """执行双路混合检索，并返回包含降级诊断信息的完整产出对象 HybridOutcome。

        执行流程：
        1. 快速短路剪枝（空查询或 limit <= 0 返回空）；
        2. 动态计算候选池大小 candidates = max(limit, candidate_limit)；
        3. 执行关键字检索；
        4. 执行语义检索，并根据策略在后端缺失或异常时捕获降级告警；
        5. 调用 rrf_fuse 执行多路倒数排序融合；
        6. 调用 reranker 精细重排，并截取最终 limit 条结果。

        :param query: 搜索问句
        :param limit: 最终返回的结果切片数量上限
        :param filters: 可选的高级元数据过滤条件
        :return: 包含切片元组与告警元组的 HybridOutcome 对象
        :raises EmbeddingError: 当 on_semantic_failure='strict' 且语义服务异常时抛出
        """
        self.last_warnings = ()
        # 1. 快速短路剪枝
        if not query.strip() or limit <= 0:
            return HybridOutcome(())

        # 2. 超额抓取候选池：确保即使单路数量不足，也能为 RRF 融合与重排提供足够的候选深度
        candidates = max(limit, self.candidate_limit)
        keyword_results = self.keyword.search(query, limit=candidates, filters=filters)
        vector_results: list[SearchResult] = []
        warnings: list[str] = []

        # 3. 语义检索分支：支持 None 判空与异常安全降级
        if self.semantic is None:
            warning = self.semantic_unavailable_reason
            if self.on_semantic_failure == "strict":
                raise EmbeddingError(warning)
            warnings.append(warning)
        else:
            try:
                vector_results = self.semantic.search(query, limit=candidates, filters=filters)
            except Exception as exc:
                if self.on_semantic_failure == "strict":
                    raise
                # 记录详细的异常类型与报错信息，平滑回退
                warnings.append(
                    f"Semantic backend failed ({type(exc).__name__}: {exc}); using keyword results"
                )

        # 记录降级日志
        for warning in warnings:
            logger.warning("Hybrid retrieval degraded: %s", warning)

        # 4. 多路倒数排序融合（RRF）：
        # 若语义分支降级为空，RRF 会自动按 keyword 单路打分，无缝降级为纯关键词检索
        fused = rrf_fuse(
            {"keyword": keyword_results, "semantic": vector_results},
            rank_constant=self.rank_constant,
        )

        # 5. 二次精细重排并最终截断
        results = tuple(self.reranker.rerank(query, fused, limit)[:limit])
        self.last_warnings = tuple(warnings)
        return HybridOutcome(results, self.last_warnings)

    def search(
        self,
        query: str,
        limit: int = 10,
        filters: SearchFilters | None = None,
    ) -> list[SearchResult]:
        """标准 Retriever 协议契约接口，透明代理并返回切片结果列表。

        :param query: 搜索问句
        :param limit: 最大返回切片数（默认 10）
        :param filters: 可选的高级元数据过滤条件
        :return: 排序后的 SearchResult 列表
        """
        return list(self.search_with_status(query, limit, filters).results)
