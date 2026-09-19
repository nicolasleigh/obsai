"""检索子系统核心领域模型与协议契约（Stable retrieval contracts & domain models）。

核心设计哲学与规范：
1. 协议化驱动设计（Protocol-Driven Structural Subtyping）：
   采用 Python 标准库的 `typing.Protocol` 定义检索器统一契约 `Retriever`。
   全文检索（FTSRetriever）、向量检索（VectorRetriever）、混合检索（HybridRetriever）及图谱扩展检索（GraphRetriever）
   均通过结构化子类型隐式实现该契约，无需多重继承耦合，极大简化了单测 Mock 与组件插拔。
2. 绝对不可变性（Frozen Pydantic Models）：
   `SearchFilters` 与 `SearchResult` 均声明为 `frozen=True` 的不可变数据模型，
   保证跨进程、异步任务及多路检索融合管线中数据的纯粹性与线程安全。
3. 统一多源出处溯源（Multi-Source Provenance Support）：
   `SearchResult` 原生提供 `sources` 元组，记录各路检索源共同命中情况，
   供前端界面展示来源标靶与诊断徽章。
"""

from typing import Protocol
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# 合法的 JSON 标量基本类型别名（支持字符串、整数、浮点数、布尔值及 None）
JsonScalar = str | int | float | bool | None


class SearchFilters(BaseModel):
    """结构化检索元数据过滤条件模型。

    封装针对笔记元数据的高级多维过滤规则（标签、目录路径、修改时间、Frontmatter 与 Dataview 属性）。
    采用不可变（frozen）设计，保证多路检索共享过滤条件时的安全性。
    """

    model_config = ConfigDict(frozen=True)

    tags: tuple[str, ...] = ()
    """需同时匹配的标签元组（如 ('python', 'asyncio')）。"""

    folder: str | None = None
    """需匹配的知识库目录相对路径前缀（如 'Tech/Backend'）。"""

    modified_after: datetime | None = None
    """笔记修改时间下界（大于等于该时间）。"""

    modified_before: datetime | None = None
    """笔记修改时间上界（小于等于该时间）。"""

    frontmatter: dict[str, JsonScalar] = Field(default_factory=dict)
    """需精确匹配的 YAML Frontmatter 标量属性字典。"""

    dataview: dict[str, str] = Field(default_factory=dict)
    """需精确匹配的 Dataview 内联属性字典。"""

    @property
    def active(self) -> bool:
        """判断当前过滤条件是否包含任何有效的过滤规则。

        若任意条件（非空标签、有效目录路径、修改时间界限、Frontmatter 或 Dataview）已设置，返回 True；
        若全部为空白/缺省，返回 False，供底层 SQL 编译器快速跳过过滤子查询联表开销。
        """
        return bool(
            self.tags
            or (self.folder and self.folder.strip("/"))
            or self.modified_after
            or self.modified_before
            or self.frontmatter
            or self.dataview
        )


class SearchResult(BaseModel):
    """统一检索命中切片实体模型。

    封装单次检索命中的切片核心信息、所属笔记元数据、高亮摘要、正向相关度得分及多路出处。
    """

    model_config = ConfigDict(frozen=True)

    chunk_id: str
    """命中切片的全局唯一标识 ID。"""

    note_id: str
    """切片所属笔记在知识库中的唯一 ID。"""

    path: str
    """切片所属笔记在知识库中的相对物理路径（例如 'Backend/Database.md'）。"""

    title: str
    """笔记标题。"""

    heading_path: list[str]
    """切片在笔记正文中所处的层级面包屑大纲路径（例如 ['架构设计', '存储层', 'WAL 机制']）。"""

    snippet: str
    """切片的高亮摘要文本（包含方括号标记或截断片段）。"""

    score: float
    """标准化后的正向相关度分值（单调递增，数值越大代表与搜索问句越相关）。"""

    source: str
    """首要命中检索源标识（例如 'keyword', 'semantic', 'hybrid', 'graph'）。"""

    sources: tuple[str, ...] = ()
    """所有命中当前切片的出处源元组（例如多路混合命中时记录 ('keyword', 'semantic')）。"""


class Retriever(Protocol):
    """检索器抽象协议（Python Structural Protocol）。

    任何实现了符合该签名的 search 方法的检索类（如 FTS、Vector、Hybrid、Graph），
    均隐式满足该协议，可透明组合与注入。
    """

    def search(
        self,
        query: str,
        limit: int = 10,
        filters: SearchFilters | None = None,
    ) -> list[SearchResult]:
        """执行检索并返回命中切片列表。

        :param query: 用户搜索问句或关键词
        :param limit: 最大返回结果切片数量（默认 10）
        :param filters: 可选的高级元数据过滤条件
        :return: 经过排序的 SearchResult 切片列表
        """
        ...
