"""知识检索子系统入口与门面模块（Retrieval Subsystem Facade）。

核心架构设计：
1. 基础契约即时就绪（Eagerly Loaded Contracts）：
   抽象基类（Retriever）与轻量级数据模型（SearchFilters, SearchResult）在包加载时立即引入，
   供外部系统作为类型标注使用，开销极小。
2. PEP 562 模块级按需延迟加载（Lazy Loading via __getattr__）：
   重量级的具体检索实现（FTS 全文、向量语义、多路混合检索、图谱拓扑检索、重排序与融合算法）
   均延后至首次显式访问模块属性时才进行局部导入。
3. 极速 CLI 冷启动与避免循环依赖：
   有效减少无需复杂检索功能（例如 obsai status, obsai version 等）时的启动耗时，
   并彻底解耦具体检索实现子模块与包入口之间的循环引用风险。
"""

from obsai.retrieval.models import Retriever, SearchFilters, SearchResult

# 显式声明公共导出 API 符号清单
__all__ = [
    # 核心检索抽象契约与数据模型
    "Retriever",
    "SearchFilters",
    "SearchResult",
    # 具体检索器实现（按需延迟加载）
    "FTSRetriever",
    "VectorRetriever",
    "HybridRetriever",
    "HybridOutcome",
    "GraphRetriever",
    # 重排器与融合算法（按需延迟加载）
    "Reranker",
    "NoOpReranker",
    "rrf_fuse",
]


def __getattr__(name: str):
    """PEP 562 模块级动态属性分发钩子。

    当调用方访问尚未绑定的模块属性时，按需执行局部导入并返回对应类或函数，
    避免提前加载重型子模块与其深层依赖。

    :param name: 被访问的模块属性名称
    :return: 对应的类、函数或对象
    :raises AttributeError: 当请求未定义的非法属性时抛出
    """
    # 延迟加载：基于 SQLite FTS5 BM25 的本地全文检索器
    if name == "FTSRetriever":
        from obsai.retrieval.fts import FTSRetriever
        return FTSRetriever

    # 延迟加载：基于向量数据库/嵌入模型的语义检索器
    if name == "VectorRetriever":
        from obsai.retrieval.vector import VectorRetriever
        return VectorRetriever

    # 延迟加载：双路混合检索器（FTS + 向量）及其产出结构
    if name in ("HybridRetriever", "HybridOutcome"):
        from obsai.retrieval.hybrid import HybridOutcome, HybridRetriever
        return {"HybridRetriever": HybridRetriever, "HybridOutcome": HybridOutcome}[name]

    # 延迟加载：基于笔记双链拓扑图谱的邻近度检索器
    if name == "GraphRetriever":
        from obsai.retrieval.graph import GraphRetriever
        return GraphRetriever

    # 延迟加载：检索结果精细化重排器接口与空操作保底实现
    if name in ("Reranker", "NoOpReranker"):
        from obsai.retrieval.reranker import NoOpReranker, Reranker
        return {"Reranker": Reranker, "NoOpReranker": NoOpReranker}[name]

    # 延迟加载：倒数排序融合（Reciprocal Rank Fusion）多路合并算法
    if name == "rrf_fuse":
        from obsai.retrieval.fusion import rrf_fuse
        return rrf_fuse

    # 未知属性回退至标准 Python AttributeError
    raise AttributeError(name)
