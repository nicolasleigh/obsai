"""Read-only graph contracts derived from the Vault's indexed WikiLinks.

该模块定义了基于 Obsidian 双向链接（WikiLinks）衍生构建的只读知识图谱核心契约模型。

核心架构与设计原则：
1. 不可变数据传输契约（Immutable Data Transfer Objects）：
   所有图谱实体均采用 @dataclass(frozen=True) 构建，集合字段使用只读 tuple，
   确保在图谱计算、并发分析与多端渲染过程中状态绝对安全且无副作用。
2. 组合爆炸安全防御（Combinatorial Explosion Defense）：
   通过 GraphLimits 强制约束子图遍历的最大深度（max_depth）、节点数（max_nodes）与边数（max_edges），
   避免在高密度链接网络中因广度优先遍历（BFS）导致内存耗尽或前端渲染卡死。
3. 细粒度 Obsidian 语法映射：
   完整保留双向链接的富文本属性，包括标题锚点（#heading）、块引用（^block-id）、
   嵌入渲染语法（![[...]]）以及自定义别名（|alias）。
4. 幽灵/断链智能识别（Phantom / Broken Link Detection）：
   GraphEdge.broken 属性可快速检测“写了链接但目标笔记尚未创建”的悬空状态，
   为孤岛分析、断链修复及笔记创建建议提供关键支撑。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class GraphLimits:
    """知识图谱遍历与邻域探索的安全边界限制。

    防止在复杂链接网络中进行无界遍历导致计算超时与内存暴涨。
    """

    max_depth: int = 2   # 局部探索的最大跳数（Hop Count，0=自身，1=直连，2=二度邻居）
    max_nodes: int = 50  # 允许收集的最大节点总数硬上限
    max_edges: int = 200 # 允许收集的最大关系边总数硬上限

    def __post_init__(self) -> None:
        """后初始化验证：保证所有限制非负，且至少允许容纳 1 个种子节点。"""
        if self.max_depth < 0 or self.max_nodes < 1 or self.max_edges < 0:
            raise ValueError("Graph limits must be nonnegative and allow at least one node")


@dataclass(frozen=True)
class GraphEdge:
    """表示从源笔记指向目标笔记（或未创建笔记）的双向链接有向关系边。"""

    id: int                          # 数据库 links 表物理自增主键
    source_note_id: str              # 发出链接的源笔记唯一标识符
    source_path: str                 # 源笔记在 Vault 中的相对路径（如 "Folder/NoteA.md"）
    source_block_id: str | None      # 源链接所在的块引用锚点 ID（若链接位于具名块内）
    target_path: str | None          # 链接文本中写入的原始目标路径（如 "NoteB"）
    target_note_id: str | None       # 解析成功的真实目标笔记 ID（若目标笔记不存在则为 None）
    resolved_target_path: str | None # 目标笔记在 Vault 中的物理绝对/相对规范路径
    target_heading: str | None       # 链接指向的标题锚点（如 "[[Note#章节一]]" 中的 "章节一"）
    target_block_id: str | None      # 链接指向的块引用锚点（如 "[[Note#^ref-1]]" 中的 "ref-1"）
    display_text: str | None         # 别名显示标签（如 "[[Note|自定义文本]]" 中的 "自定义文本"）
    is_embed: bool                   # 是否为嵌入式渲染链接（即感叹号开头的 "![[...]]"）
    position: int                    # 链接在源笔记文本内容中的起始字符偏移量（Offset）

    @property
    def broken(self) -> bool:
        """判断当前链接是否为悬空断链（目标路径存在但找不到对应的物理笔记）。"""
        return self.target_note_id is None and self.target_path is not None


@dataclass(frozen=True)
class GraphNode:
    """表示局部子图中的一个笔记顶点。"""

    note_id: str      # 笔记唯一标识符
    path: str         # 笔记在 Vault 中的相对路径
    title: str        # 笔记展示标题（从 Frontmatter 或文件名解析得到）
    distance: int     # 距离起始种子笔记的最短跳数距离（0=自身，1=一度关联，...）
    seed_note_id: str # 发起本次子图遍历的根种子笔记 ID（支持追溯来源）


@dataclass(frozen=True)
class GraphNeighborhood:
    """以指定种子笔记为中心的局部图谱邻域上下文报告。"""

    nodes: tuple[GraphNode, ...] # 局部图谱收集到的所有节点只读元组
    edges: tuple[GraphEdge, ...] # 局部图谱内节点间互相连通的所有边只读元组
    truncated: bool              # 是否因触发 GraphLimits 上限而发生了截断截流
