"""检索基准离线评测与质量度量模块（Retrieval benchmark & evaluation metrics）。

核心设计哲学：
1. 确定性与轻量无副作用（Deterministic & Zero Side-Effects）：
   纯内存数学统计与比对，无需任何外部网络或在线服务，专为 CI/CD 自动化测试与检索管线回归校验设计。
2. 粗细双粒度真值对齐（Dual-Granularity Ground Truth）：
   统一支持笔记路径级（`expected_paths`）与切片级（`expected_chunk_ids`）标准答案比对。
3. 经典信息检索三大度量指标（Core Information Retrieval Metrics）：
   - Recall@K：召回率，衡量 Top-K 结果找回期望相关项的完整程度；
   - MRR（Mean Reciprocal Rank）：平均倒数排名，衡量用户首眼看到第一个有效结果的排位优劣；
   - Precision@K：精确度，衡量 Top-K 结果列表中有效答案的信噪比。
4. 切片级排重与宏观平均（Macro-Averaging & Deduplication）：
   严格剔除同一篇笔记多个切片重复计入召回的情况，并在所有用例间求算术平均值（Macro-Average）。
"""

from dataclasses import dataclass
from typing import Iterable

from obsai.retrieval.models import Retriever


@dataclass(frozen=True)
class BenchmarkCase:
    """检索基准测试用例模型。

    封装单次测试的查询语句以及预期的真值标准答案（支持笔记路径与切片 ID 两种粒度）。
    """

    query: str
    """测试查询词或语义问句文本。"""

    expected_paths: tuple[str, ...] = ()
    """粗粒度期望真值：期望命中的笔记相对路径元组（只要命中该笔记下的任意切片即算命中）。"""

    expected_chunk_ids: tuple[str, ...] = ()
    """细粒度期望真值：期望命中的特定切片唯一标识 ID 元组。"""


@dataclass(frozen=True)
class RetrievalMetrics:
    """检索指标评估结果模型。

    封装对一组测试用例进行评测后的跨用例宏观平均指标（Macro-Averaged Metrics）。
    """

    recall_at_k: float
    """Top-K 召回率（Recall@K）：找回的期望相关项总数与所有期望项总数的比值均值。"""

    mrr: float
    """平均倒数排名（MRR, Mean Reciprocal Rank）：首个相关结果位次倒数（1 / rank）的均值。"""

    precision_at_k: float
    """Top-K 精确率（Precision@K）：Top-K 检索列表中相关结果数量占 K 的比值均值。"""


def evaluate(retriever: Retriever, cases: Iterable[BenchmarkCase], *, k: int = 5) -> RetrievalMetrics:
    """针对指定的检索器实例运行基准测试集，评估并输出 Top-K 检索性能指标。

    执行流程：
    1. 参数校验：校验 k >= 1 且用例集合非空；
    2. 遍历测试用例，统一构建期望真值集合；
    3. 调用 retriever.search 执行查询并截断至前 K 条；
    4. 逐条比对路径与切片，完成去重统计、首命中排名捕捉；
    5. 计算单用例的 Recall@K、Reciprocal Rank 与 Precision@K；
    6. 计算所有用例的宏观平均值（Macro-Average）并返回。

    :param retriever: 被评估的检索器实例（支持 FTS、Vector、Hybrid 等各类 Retriever）
    :param cases: 包含测试查询与标准答案的基准用例可迭代序列
    :param k: 评测截断深度（Top-K，默认取前 5 条）
    :return: 包含 Recall@K、MRR 和 Precision@K 的评估度量结果
    :raises ValueError: 当 k < 1、用例集为空或用例未指定任何期望真值时抛出
    """
    # 1. 截断深度参数校验
    if k < 1:
        raise ValueError("K must be positive")
    # 固化用例序列，确保支持多次遍历与长度计算
    cases = tuple(cases)
    if not cases:
        raise ValueError("Benchmark requires at least one case")

    recalls: list[float] = []
    reciprocals: list[float] = []
    precisions: list[float] = []

    # 2. 逐个用例执行检索与统计
    for case in cases:
        # 构造统一的期望真值集合（包含路径元组与切片元组）
        expected = {("path", path) for path in case.expected_paths} | {
            ("chunk", chunk_id) for chunk_id in case.expected_chunk_ids
        }
        if not expected:
            raise ValueError("Benchmark case needs an expected note path or chunk ID")

        found: set[tuple[str, str]] = set()
        first_rank = None
        relevant_results = 0

        # 执行检索并严格截断取前 k 条，排名从 1 开始
        for rank, result in enumerate(retriever.search(case.query, limit=k)[:k], start=1):
            # 比对当前结果的路径与切片 ID 是否命中期望集合
            matches = expected & {("path", result.path), ("chunk", result.chunk_id)}
            # 剔除先前已匹配过的真值项，防止同一真值被多次切片重复计算召回
            new_matches = matches - found
            if new_matches:
                found.update(new_matches)
                relevant_results += 1
                # 记录首个命中相关项的位次（用于计算倒数排名 RR）
                if first_rank is None:
                    first_rank = rank

        # 3. 计算单用例指标
        # 召回率：已找到的期望项数量 / 总期望项数量
        recalls.append(len(found) / len(expected))
        # 倒数排名：首个命中的 1/rank，若未命中则为 0.0
        reciprocals.append(1.0 / first_rank if first_rank is not None else 0.0)
        # 精确率：命中的相关结果数量 / 固定窗口大小 K
        precisions.append(relevant_results / k)

    # 4. 计算宏观平均值（Macro-Average）并构造返回对象
    count = len(cases)
    return RetrievalMetrics(
        recall_at_k=sum(recalls) / count,
        mrr=sum(reciprocals) / count,
        precision_at_k=sum(precisions) / count,
    )
