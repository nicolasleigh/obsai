"""Read-only semantic and graph-backed WikiLink candidates.

该模块是知识图谱子系统的双向链接智能推荐引擎（LinkSuggester），
通过融合“向量语义检索（Semantic Search）”与“图谱拓扑邻域（Graph Neighborhood）”，
为用户当前笔记推荐最值得建立链接（WikiLinks）的相关笔记候选。

核心架构与设计原则：
1. 语义与拓扑混合打分模型（Hybrid Fusion Scoring）：
   - 语义初筛：利用笔记标题与前文进行向量语义相似度搜索；
   - 拓扑增强：计算候选在知识图谱中与当前笔记的最短距离（distance）；
   - 融合公式：Score = 1.0 / (rank + 1) + 0.25 / distance，兼顾语义相关性与知识网络亲密度；
   - 拓扑兜底：对紧密处于 1~2 跳邻域但未被语义捕捉的结构强相关笔记赋予独立拓扑加权推荐。
2. 实时文档解析（Zero-Staleness Freshness）：
   直接通过 parse_note 实时读取物理磁盘上的当前笔记正文，即写即推，
   无需等待后台异步索引作业更新。
3. 严格的多重安全与去重防护：
   - 路径沙箱防御：集成 SafeWriteService 防范路径穿越；
   - 语法安全过滤：剔除文件名中带有 []|# 等破坏 WikiLink 语法符号的目标；
   - 多形态已建链接判重：智能识别裸文件名（stem）、相对路径与带 .md 后缀的三重形式，杜绝重复推荐已存在的链接；
   - 排除自引用：严格禁止笔记向自身建立链接。
4. 结果确定性与可解释性：
   每个推荐实体均附带人类可读的 reason 归因，并以得分降序、路径升序进行双关键字稳定排序。
"""

from dataclasses import dataclass
from pathlib import PurePosixPath, Path

from obsai.graph.service import GraphService
from obsai.retrieval.models import Retriever
from obsai.safe_write.service import SafeWriteService
from obsai.storage.repositories import IndexRepository
from obsai.vault.parser import parse_note
from obsai.vault.scanner import scan_markdown_files


@dataclass(frozen=True)
class LinkSuggestion:
    """双向链接推荐结果不可变实体。"""

    path: str    # 推荐目标笔记在 Vault 中的相对路径
    title: str   # 目标笔记展示标题
    score: float # 混合推荐相关性得分（越高越相关）
    reason: str  # 推荐归因说明（如 "Semantic match; graph distance 1"）

    @property
    def wikilink(self) -> str:
        """根据目标路径自动生成标准的 Obsidian [[WikiLink]] 引用语法。"""
        target = self.path[:-3] if self.path.lower().endswith(".md") else self.path
        return f"[[{target}]]"


class LinkSuggester:
    """双向链接推荐引擎，结合语义相似度与图谱拓扑距离输出高质量推荐。"""

    def __init__(self, vault_root: Path, repository: IndexRepository,
                 graph: GraphService, semantic: Retriever):
        """初始化链接推荐引擎。

        :param vault_root: Obsidian 知识库物理根路径
        :param repository: 知识库索引数据库仓储
        :param graph: 图谱服务实例（用于获取邻域拓扑）
        :param semantic: 语义检索器实例（用于向量相似度搜索）
        """
        self.safe = SafeWriteService(vault_root)
        self.root = self.safe.root
        self.repository = repository
        self.graph = graph
        self.semantic = semantic

    @staticmethod
    def _safe_target(path: str) -> bool:
        """校验目标路径是否符合 WikiLink 语法安全性，禁止包含破坏语法的定界符与换行。"""
        return not any(character in path for character in "[]|#\r\n")

    @staticmethod
    def _already_linked(source_path: str, target_path: str, existing: set[str]) -> bool:
        """智能判定目标笔记是否已经在源笔记的正文中被链接过。

        全面覆盖 Obsidian 支持的 3 种链接书写形式：
        1. 纯文件名简写：如 [[NoteB]]；
        2. 全相对路径：如 [[Folder/NoteB]]；
        3. 同级相对路径：如源文件在 Folder/NoteA.md，引用写为 [[NoteB]]。
        """
        target_no_suffix = target_path[:-3] if target_path.lower().endswith(".md") else target_path
        target_stem = PurePosixPath(target_no_suffix).name
        parent = PurePosixPath(source_path).parent
        for value in existing:
            normalized = value[:-3] if value.lower().endswith(".md") else value
            if normalized in (target_no_suffix, target_stem):
                return True
            if str(parent / normalized) == target_no_suffix:
                return True
        return False

    def suggest(self, path: str, *, limit: int = 10) -> list[LinkSuggestion]:
        """为指定笔记计算并返回 Top-K 篇潜在推荐链接候选。

        :param path: 源笔记相对路径
        :param limit: 最多返回的推荐数量，默认 10
        :return: 经过打分排序的 LinkSuggestion 列表
        """
        # 短路保护
        if limit <= 0:
            return []

        # 路径安全沙箱校验与最新正文实时解析
        source = self.safe.path(path)
        note = parse_note(source, vault_root=self.root)

        # 构造语义查询：结合标题与前 2000 字符纯文本
        query = (note.title + "\n" + note.plain_text[:2000]).strip()
        # 过采样检索，为后续各级过滤保留足够的候选余量
        results = self.semantic.search(query, limit=min(100, max(30, limit * 3)))

        # 查询源笔记在图谱中的 2 跳拓扑邻域
        indexed = self.repository.notes.get_by_path(note.path)
        neighborhood = self.graph.get_neighbors(indexed.id, depth=2) if indexed else None
        distance_by_id = {item.note_id: item.distance for item in neighborhood.nodes} if neighborhood else {}

        # 收集源笔记中已存在的链接集合与磁盘可见的真实 Markdown 文件集合
        existing = {link.target_path for link in note.wikilinks if link.target_path}
        visible = {item.relative_to(self.root).as_posix() for item in scan_markdown_files(self.root)}

        candidates: dict[str, tuple[float, str]] = {}

        # 阶段 1：遍历语义检索结果，计算“语义倒数排名 + 图拓扑亲近度”综合分
        for rank, result in enumerate(results):
            # 过滤：自身笔记、非可见文件、非法文件名
            if result.path == note.path or result.path not in visible or not self._safe_target(result.path):
                continue
            # 过滤：已经建立过链接的笔记
            if self._already_linked(note.path, result.path, existing):
                continue

            distance = distance_by_id.get(result.note_id)
            # 综合打分：语义排名倒数分 (1/(rank+1)) + 图距离加成 (0.25/distance)
            score = 1.0 / (rank + 1) + (0.25 / distance if distance else 0.0)
            reason = "Semantic match" + (f"; graph distance {distance}" if distance else "")

            current = candidates.get(result.note_id)
            if current is None or score > current[0]:
                candidates[result.note_id] = (score, reason)

        # 阶段 2：遍历图谱邻域节点，对未被语义召回但拓扑临近的笔记补充兜底推荐
        if neighborhood:
            for node in neighborhood.nodes:
                # 排除自身（distance=0）、非可见文件、非法路径
                if node.distance == 0 or node.path not in visible or not self._safe_target(node.path):
                    continue
                # 排除已链接笔记
                if self._already_linked(note.path, node.path, existing):
                    continue

                score, reason = candidates.get(node.note_id, (0.0, ""))
                # 若此前未被语义命中，赋予纯图谱拓扑得分
                if score == 0.0:
                    candidates[node.note_id] = (0.15 / node.distance,
                                                f"Graph distance {node.distance}")

        # 阶段 3：装配结果实体并执行双关键字稳定排序
        suggestions = []
        for note_id, (score, reason) in candidates.items():
            record = self.repository.notes.get(note_id)
            if record is not None and record.path != note.path:
                suggestions.append(LinkSuggestion(record.path, record.title, score, reason))

        # 优先按得分降序 (-item.score)，得分相同时按路径升序 (item.path) 保持确定性
        return sorted(suggestions, key=lambda item: (-item.score, item.path))[:limit]
