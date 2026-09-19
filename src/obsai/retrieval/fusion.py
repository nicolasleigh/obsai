"""确定性倒数排序融合算法模块（Deterministic Reciprocal Rank Fusion, RRF）。

核心设计哲学与算法优势：
1. 零尺度依赖（Scale-Invariant Multi-Source Fusion）：
   RRF（由 Cormack 等人于 SIGIR 2009 提出）仅依据切片在各检索器中的“排名位次（Rank）”进行打分融合，
   无需关注不同检索系统（如 SQLite FTS5 的 BM25 得分与向量数据库的 Cosine 余弦相似度）的绝对数值范围与量纲差异，
   彻底规避了传统线性加权中脆弱易受噪声影响的 Min-Max 或 Z-Score 归一化步骤。
2. 多路共现自然加权（Co-occurrence Boost）：
   计算公式为 score(d) = sum(1 / (k + rank))。当某个切片被关键字检索与向量检索同时召回时，
   其分值将获得双重甚至多重累加，使跨模型交叉认可的核心结果自然浮现至顶端。
3. 严格确定性与可复现性（Strict Determinism & Reproducibility）：
   - 对检索源名称强制按字典序升序遍历（`sorted(sources)`），杜绝因调用方字典键插入顺序不同导致的结果微小抖动；
   - 采用“综合得分降序 -> 单源最佳名次升序 -> 切片 ID 字典序”的三级复合排序键，平局场景下保持 100% 确定性。
4. 单源排重与出处溯源（Deduplication & Provenance Tracking）：
   单个检索器内部返回重复项时仅计入其首次最高排名；融合后的实体统一记录 `sources` 元组（如 ('keyword', 'semantic')），
   供前端交互界面展示命中来源徽章。
"""

from collections.abc import Mapping, Sequence

from obsai.retrieval.models import SearchResult


def rrf_fuse(
    sources: Mapping[str, Sequence[SearchResult]], *, rank_constant: int = 60
) -> list[SearchResult]:
    """对多路检索源的结果序列执行确定性倒数排序融合（Reciprocal Rank Fusion, RRF）。

    算法流程：
    1. 校验平滑常数合法性（k >= 1）；
    2. 按检索源名称升序遍历，确保执行顺序与字典插入顺序无关；
    3. 针对每个检索源的结果列表执行内部排重，计算 1.0 / (rank_constant + rank) 累计得分；
    4. 记录代表性实体、各切片命中的出处集合（provenance）及全源最佳名次；
    5. 执行三级稳定复合排序：(-score, best_rank, chunk_id)；
    6. 装配并返回更新了 score、source='hybrid' 和 sources 出处元组的 SearchResult 列表。

    :param sources: 各路检索源名称到命中结果列表的映射（例如 {'keyword': [...], 'semantic': [...]}）
    :param rank_constant: RRF 平滑常数 k（默认 60，有效防止首位过度霸榜，平衡长尾召回）
    :return: 经过倒数排名加权融合、去重与确定性排序后的 SearchResult 列表
    :raises ValueError: 当 rank_constant < 1 时抛出
    """
    # 1. 平滑常数校验（工业界与学术界标准推荐值通常为 60）
    if rank_constant < 1:
        raise ValueError("RRF rank constant must be positive")

    # 累计 RRF 融合得分字典 {chunk_id: cumulative_score}
    scores: dict[str, float] = {}
    # 首个命中的切片代表性实体 {chunk_id: SearchResult}
    representatives: dict[str, SearchResult] = {}
    # 切片召回来源集合 {chunk_id: {'keyword', 'semantic', ...}}
    provenance: dict[str, set[str]] = {}
    # 切片在任意单源中获得的最佳名次（数值越小越靠前）{chunk_id: min_rank}
    best_rank: dict[str, int] = {}

    # 2. 排序检索源名称：保证相同输入下的遍历顺序与调用方 Mapping 的键插入顺序完全无关
    for source in sorted(sources):
        seen: set[str] = set()
        # 排名从 1 开始（第 1 名，第 2 名……）
        for rank, result in enumerate(sources[source], start=1):
            # 单源内去重：若同一检索器返回了多次相同切片，仅保留最高位次
            if result.chunk_id in seen:
                continue
            seen.add(result.chunk_id)

            # 3. 核心 RRF 倒数累加打分：score += 1 / (k + rank)
            scores[result.chunk_id] = scores.get(result.chunk_id, 0.0) + 1.0 / (
                rank_constant + rank
            )
            # 保留首次出现的结果对象作为正文元数据载体
            representatives.setdefault(result.chunk_id, result)
            # 记录出处来源
            provenance.setdefault(result.chunk_id, set()).add(source)
            # 维护最佳名次（用于平局决胜）
            best_rank[result.chunk_id] = min(best_rank.get(result.chunk_id, rank), rank)

    # 4. 三级复合排序保证绝对确定性：
    # 优先级 1：-scores[chunk_id]（综合得分降序）
    # 优先级 2：best_rank[chunk_id]（若总分相同，在单一源中取得更高名次者优先）
    # 优先级 3：chunk_id（保底决胜，按切片 ID 字符串升序）
    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], best_rank[chunk_id], chunk_id))

    # 5. 复制并更新模型字段，构建最终混合检索切片列表
    return [
        representatives[chunk_id].model_copy(update={
            "score": scores[chunk_id],
            "source": "hybrid",
            "sources": tuple(sorted(provenance[chunk_id])),
        })
        for chunk_id in ordered
    ]
