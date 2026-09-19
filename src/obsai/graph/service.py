"""Deterministic, bounded bidirectional graph traversal.

该模块是知识图谱子系统的核心业务服务层（GraphService），
负责执行确定性、有界限的双向广度优先搜索（BFS）图遍历，
提供邻域发现、反向链接提取与知识拓扑分析功能。

核心架构与设计原则：
1. 真双向拓扑感知（Bidirectional Awareness）：
   在图遍历中，将正向出链（outgoing）与反向入链（backlinks）合并扩展，
   使知识探索不仅能顺着“A 引用了 B”正向探索，也能逆向感知“B 被谁引用”，
   完整捕获知识网络中的强相关子图。
2. 组合爆炸安全防御（Bounded BFS Traversal）：
   受 GraphLimits 约束，严格对遍历跳数深度（depth）、节点总量（max_nodes）与边总量（max_edges）
   实施三重防护熔断，并附带 truncated 标记，防止稠密链接网络中的无界扩散。
3. 多源种子溯源（Multi-seed Provenance Tracking）：
   支持同时传入多个种子笔记进行子图扩展，通过 seed_note_id 精准追踪每个节点是由哪篇种子笔记衍生出来的。
4. 无环死锁与去重保护（Cycle Avoidance）：
   借助 nodes 与 edges 哈希字典，天然杜绝网状知识网络中自环（A -> A）或环路（A -> B -> A）引发的无限递归。
5. 超限预探测技术（Over-fetching Probe）：
   查询出链和反链时主动多查 1 条（fetch_limit = max_edges + 1），零额外性能损耗地探明底层是否存在溢出数据。
"""

from collections import deque
from typing import Sequence

from obsai.graph.models import GraphEdge, GraphLimits, GraphNeighborhood, GraphNode
from obsai.graph.repository import GraphRepository
from obsai.errors import VaultError
from obsai.storage.repositories import IndexRepository, NoteRecord


class GraphService:
    """知识图谱核心服务，提供多态笔记解析、出链/反链提取与有界双向邻域探索。"""

    def __init__(self, repository: IndexRepository, graph_repository: GraphRepository,
                 *, limits: GraphLimits = GraphLimits()):
        """初始化图谱领域服务。

        :param repository: 索引仓储（提供 notes 笔记访问）
        :param graph_repository: 图谱仓储（提供 links 链接访问）
        :param limits: 图谱探索的安全边界配置
        """
        self.notes = repository.notes
        self.links = graph_repository
        self.limits = limits

    def resolve(self, note: str) -> NoteRecord:
        """多态笔记解析器：支持通过笔记物理相对路径或内部 ID 灵活解析笔记记录。

        :param note: 笔记相对路径（如 "Notes/Work.md"）或唯一 ID
        :return: 对应的 NoteRecord 实体
        :raises VaultError: 若笔记未被索引或文件不存在时抛出
        """
        record = self.notes.get_by_path(note) or self.notes.get(note)
        if record is None:
            raise VaultError(f"Unknown indexed note: {note}")
        return record

    def get_backlinks(self, note: str) -> list[GraphEdge]:
        """获取指向指定笔记的所有反向入边（Backlinks）。

        :param note: 目标笔记路径或 ID
        :return: 反向链接边列表
        """
        return self.links.backlinks(self.resolve(note).id)

    def get_outgoing_links(self, note: str) -> list[GraphEdge]:
        """获取指定笔记正文中发出的所有正向出边（WikiLinks）。

        :param note: 源笔记路径或 ID
        :return: 正向链接边列表
        """
        return self.links.outgoing(self.resolve(note).id)

    def get_neighbors(self, note: str, depth: int | None = None) -> GraphNeighborhood:
        """获取单篇笔记在指定跳数深度内的局部图谱邻域上下文（语法糖快捷方法）。

        :param note: 起始种子笔记路径或 ID
        :param depth: 可选的探索深度跳数，默认取 min(2, limits.max_depth)
        :return: 局部图谱邻域报告 GraphNeighborhood
        """
        return self.expand([note], depth=min(2, self.limits.max_depth) if depth is None else depth)

    def expand(self, notes: Sequence[str], *, depth: int = 2) -> GraphNeighborhood:
        """从一个或多个种子笔记出发，执行有界的双向广度优先搜索（BFS）以发现局部子图。

        :param notes: 起始种子笔记序列（路径或 ID）
        :param depth: 探索的最大跳数深度（必须在 [0, limits.max_depth] 之间）
        :return: 包含节点集合、边集合与截断状态的 GraphNeighborhood 报告
        :raises ValueError: 探索深度超出限制范围
        """
        # 前置参数校验
        if depth < 0 or depth > self.limits.max_depth:
            raise ValueError(f"Graph depth must be between 0 and {self.limits.max_depth}")

        nodes: dict[str, GraphNode] = {}              # note_id -> GraphNode 映射字典，实现节点去重
        edges: dict[int, GraphEdge] = {}              # edge_id -> GraphEdge 映射字典，实现边去重
        queue: deque[tuple[str, int, str]] = deque()  # FIFO 工作队列：(当前笔记ID, 当前跳数距离, 来源种子笔记ID)
        truncated = False                             # 截断状态标识

        # 阶段 1：初始化并装载种子节点
        for note in notes:
            record = self.resolve(note)
            if record.id in nodes:
                continue
            # 种子数量达到节点上限检查
            if len(nodes) >= self.limits.max_nodes:
                truncated = True
                break
            nodes[record.id] = GraphNode(record.id, record.path, record.title, 0, record.id)
            queue.append((record.id, 0, record.id))

        # 阶段 2：BFS 队列遍历展开
        while queue:
            note_id, distance, seed_id = queue.popleft()
            # 已达到最大跳数深度，停止向下探索其邻居
            if distance >= depth:
                continue

            # 超限预探测：故意多查 1 条（max_edges + 1）以无额外开销判定是否超限
            fetch_limit = self.limits.max_edges + 1
            outgoing = self.links.outgoing(note_id, limit=fetch_limit)
            backlinks = self.links.backlinks(note_id, limit=fetch_limit)
            if len(outgoing) == fetch_limit or len(backlinks) == fetch_limit:
                truncated = True

            # 双向合流：出链与反链共同作为邻接边
            adjacent = outgoing + backlinks
            for edge in adjacent:
                # 边的去重与容量防护
                if edge.id not in edges:
                    if len(edges) >= self.limits.max_edges:
                        truncated = True
                        break
                    edges[edge.id] = edge

                # 推导对端邻居节点 ID（如果是出边则取 target，如果是反链则取 source）
                neighbor_id = (edge.target_note_id if edge.source_note_id == note_id
                               else edge.source_note_id)

                # 断链过滤与环路防护：目标不存在（幽灵断链）或已加入节点集合则跳过
                if neighbor_id is None or neighbor_id in nodes:
                    continue

                # 节点容量限制检查
                if len(nodes) >= self.limits.max_nodes:
                    truncated = True
                    continue

                record = self.notes.get(neighbor_id)
                if record is None:
                    continue

                # 记录新邻居节点并压入队列供下一层级遍历
                nodes[record.id] = GraphNode(record.id, record.path, record.title,
                                             distance + 1, seed_id)
                queue.append((record.id, distance + 1, seed_id))

            # 边数达到上限且队列仍有待处理节点时提前终止
            if len(edges) >= self.limits.max_edges and queue:
                truncated = True
                break

        # 打包不可变元组并返回子图报告
        return GraphNeighborhood(tuple(nodes.values()), tuple(edges.values()), truncated)
