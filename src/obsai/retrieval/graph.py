"""知识图谱拓扑扩展检索服务（Graph-Augmented Retriever / GraphRAG）。

核心设计哲学与算法架构：
1. 种子检索与双链拓扑扩散（Seed Retrieval & WikiLink Bounded Expansion）：
   以基础检索器（通常为 FTS + Vector 的 HybridRetriever）初筛命中的高相关笔记作为种子（Seeds），
   沿着用户在 Obsidian 中建立的 WikiLink 双链拓扑向外扩散遍历有界的 $k$ 度邻域，
   捕获纯文本与向量检索难以感知的隐式关联笔记。
2. 细粒度锚点感知与最佳切片优选（Anchor-Aware Chunk Selection）：
   当扩展至某篇邻居笔记时，分析所有连接到该笔记的边属性（如指向特定章节的 `#Heading` 或特定段落块的 `^block-id`），
   通过多维加权打分，精准提取被引用定位的最佳段落切片（Best Chunk），而非泛泛返回全文首页。
3. 二维复合衰减打分模型（2D Dual Decay Scoring Model）：
   融合拓扑距离与种子质量两重视角：score = 1.0 / ((seed_rank + 1) * (distance + 1))。
   跳数越远、源头种子初筛名次越靠后，则邻居切片的得分衰减越多。
4. 元数据过滤继承与确定性排序（Filter Inheritance & Deterministic Output）：
   扩展出的所有邻居节点均严格继承并校验当前请求的 `SearchFilters`（标签、目录等）；
   最终融合结果采用三级稳定键（-score, path, chunk_id）排序截断。
"""

import re

from obsai.graph import GraphNeighborhood, GraphService
from obsai.retrieval.models import Retriever, SearchFilters, SearchResult
from obsai.storage.repositories import IndexRepository
from obsai.telemetry import measured


class GraphRetriever:
    """知识图谱拓扑扩展检索器，实现标准 Retriever 协议。"""

    def __init__(self, seeds: Retriever, graph: GraphService, repository: IndexRepository,
                 *, seed_limit: int = 12, depth: int = 2):
        """初始化图谱扩展检索器。

        :param seeds: 基础种子检索器（通常为 HybridRetriever）
        :param graph: 知识图谱拓扑服务
        :param repository: 索引元数据仓储
        :param seed_limit: 参与图扩展的种子切片数量上限（默认 12）
        :param depth: 向外扩展的双链跳数/深度（默认 2 跳，最大受限于 graph.limits.max_depth）
        :raises ValueError: 当参数越界或超出图谱服务最大深度限制时抛出
        """
        # 参数边界防御性校验
        if seed_limit < 1 or depth < 0 or depth > graph.limits.max_depth:
            raise ValueError("Invalid graph retrieval limits")
        self.seeds = seeds
        self.graph = graph
        self.repository = repository
        self.seed_limit = seed_limit
        self.depth = depth
        # 缓存底层检索器抛出的警告信息与最近一次生成的子图邻域
        self.last_warnings: tuple[str, ...] = ()
        self.last_neighborhood: GraphNeighborhood | None = None

    @staticmethod
    def _best_chunk(repository: IndexRepository, note_id: str, query: str,
                    headings: set[str], block_ids: set[str]):
        """从关联邻居笔记中挑选最契合被链接上下文与检索词的最佳切片（Best Chunk）。

        四级优选打分规则：
        1. 块链接锚定（权重 8）：若切片精确包含双链引用的 target_block_id，赋予最高权重；
        2. 章节标题锚定（权重 5）：若切片的 heading_path 包含双链引用的 target_heading，赋予高权重；
        3. 关键词重合度（权重 1/词）：切片正文与搜索问句分词后的词频重叠数；
        4. 位置保底（-position）：以上维度均相同时，优先选取靠前的切片。

        :param repository: 索引仓储
        :param note_id: 候选邻居笔记 ID
        :param query: 搜索问句文本
        :param headings: 连接边中指向该笔记的章节标题集合
        :param block_ids: 连接边中指向该笔记的块唯一标识 ID 集合
        :return: 评分最高的 ChunkRecord，若无切片则返回 None
        """
        chunks = repository.chunks.list_for_note(note_id)
        if not chunks:
            return None
        # 提取搜索词的 Unicode 词条集合
        terms = set(re.findall(r"[^\W_]+", query.lower(), re.UNICODE))

        def rank(chunk):
            heading = " > ".join(chunk.heading_path).lower()
            content = chunk.embedding_text.lower()
            return (
                # 优先级 1：块链接精确命中（权重 8）
                8 if chunk.block_id and chunk.block_id in block_ids else 0,
                # 优先级 2：章节标题锚点命中（权重 5）
                5 if any(item.lower() in heading for item in headings) else 0,
                # 优先级 3：正文查询词覆盖数量
                sum(term in content for term in terms),
                # 优先级 4：切片物理位次（负数升序，靠前者优先）
                -chunk.position,
            )

        return max(chunks, key=rank)

    @measured("retrieval.graph")
    def search(self, query: str, limit: int = 10,
               filters: SearchFilters | None = None) -> list[SearchResult]:
        """执行图谱增强检索：混合检索初筛种子 -> WikiLink 拓扑扩散 -> 切片优选 -> 二维衰减打分。

        执行流程：
        1. 快速短路：空查询或 limit <= 0 时直接返回空；
        2. 种子检索：调用 seeds.search 获取初筛结果，并将分数重整为 1 / (rank + 1)；
        3. 图谱扩散：调用 graph.expand 向外遍历 depth 跳双链邻域；
        4. 邻居过滤：跳过种子本身，并严格执行 filters 元数据匹配校验；
        5. 边属性分析与切片优选：收集连接边上的章节/块锚点，调用 _best_chunk 精准定位切片；
        6. 二维衰减打分：结合种子位次与拓扑距离计算 score，组装 SearchResult；
        7. 排序截断：按 (-score, path, chunk_id) 进行确定性排序并截取 limit 条返回。

        :param query: 检索问句
        :param limit: 最大返回切片数（默认 10）
        :param filters: 可选的高级元数据过滤条件
        :return: 包含种子与图扩展命中切片的 SearchResult 列表
        """
        self.last_neighborhood = None
        self.last_warnings = ()
        # 1. 快速短路剪枝
        if not query.strip() or limit <= 0:
            return []

        # 2. 确定初筛种子数量并执行初筛检索
        seed_count = min(max(limit, self.seed_limit), self.graph.limits.max_nodes)
        seeds = self.seeds.search(query, limit=seed_count, filters=filters)[:seed_count]
        self.last_warnings = tuple(getattr(self.seeds, "last_warnings", ()))
        if not seeds:
            return []

        # 记录各种子笔记首次出现时的位次（0-based）与重整得分
        ranked_seed_notes: dict[str, int] = {}
        seed_results: list[SearchResult] = []
        for rank, item in enumerate(seeds):
            ranked_seed_notes.setdefault(item.note_id, rank)
            # 种子切片得分平滑为名次倒数：1 / (rank + 1)
            seed_results.append(item.model_copy(update={"score": 1.0 / (rank + 1)}))

        # 3. 沿着 WikiLink 边向外扩散遍历有界邻域子图
        neighborhood = self.graph.expand(list(ranked_seed_notes), depth=self.depth)
        self.last_neighborhood = neighborhood
        # 以 chunk_id 为键构建字典，预置所有初筛种子
        by_chunk = {item.chunk_id: item for item in seed_results}

        # 4. 遍历扩展子图中的所有节点
        for node in neighborhood.nodes:
            # 排除距离为 0 的种子自身或已在种子列表中的笔记
            if node.distance == 0 or node.note_id in ranked_seed_notes:
                continue
            # 继承元数据过滤规则：邻居笔记必须同样满足用户设定的过滤条件
            if not self.graph.links.note_matches(node.note_id, filters):
                continue

            # 5. 分析连接该邻居节点的边属性（收集 target_heading 与 block_id）
            touching = [edge for edge in neighborhood.edges
                        if edge.target_note_id == node.note_id or edge.source_note_id == node.note_id]
            headings = {edge.target_heading for edge in touching
                        if edge.target_note_id == node.note_id and edge.target_heading}
            block_ids = {edge.target_block_id for edge in touching
                         if edge.target_note_id == node.note_id and edge.target_block_id}
            block_ids.update(edge.source_block_id for edge in touching
                             if edge.source_note_id == node.note_id and edge.source_block_id)

            # 定位该邻居笔记中被引用的最佳段落切片
            chunk = self._best_chunk(self.repository, node.note_id, query, headings, block_ids)
            if chunk is None:
                continue

            # 6. 二维复合衰减打分：
            # 结合激发它的种子笔记排名 seed_rank 与扩散跳数 node.distance 进行双重衰减
            seed_rank = ranked_seed_notes.get(node.seed_note_id, len(seeds))
            score = 1.0 / ((seed_rank + 1) * (node.distance + 1))

            # 组装图谱检索切片结果，来源标记为 'graph'
            by_chunk.setdefault(chunk.chunk_id, SearchResult(
                chunk_id=chunk.chunk_id, note_id=node.note_id, path=node.path,
                title=node.title, heading_path=list(chunk.heading_path),
                snippet=chunk.raw_content[:240], score=score, source="graph",
                sources=("graph",),
            ))

        # 7. 全量结果三级稳定复合排序：得分降序 -> 路径升序 -> 切片 ID 升序，并截取 limit 条
        return sorted(by_chunk.values(), key=lambda item: (-item.score, item.path, item.chunk_id))[:limit]

