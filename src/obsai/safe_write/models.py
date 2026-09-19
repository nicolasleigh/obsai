"""安全写入子系统不可变变更模型（Immutable single-file change proposals）。

核心设计哲学与安全架构：
1. 不可变预演提案（Immutable Dry-Run Proposals）：
   在对磁盘进行任何物理写操作之前，系统先通过分析计算生成强类型、不可变（frozen）的 FileChange 对象，
   用于向用户输出高亮 Diff、进行人工审查确认，或打包转交给两阶段事务提交系统。
2. 乐观并发控制与防脏写（CAS Concurrency Control via original_hash）：
   记录操作发生前目标文件在磁盘上的 SHA-256 哈希（original_hash），
   在最终物理写入的关键区重新计算比对，杜绝覆盖用户在 Obsidian 等外部编辑器中最新保存的并发修改。
3. 反向链接影响透明化（Backlink Impact Transparency）：
   移动或重命名笔记时，在提案中即时推导并携带受影响的外部反链列表（affected_backlinks），
   使影响范围透明可查，并作为事务引擎级联重写全库双链的依据。
4. 跨环境高保真视觉呈现（Byte-for-Byte Visual Determinism via PreviewLine）：
   通过 PreviewLine 显式承载文本、Rich 样式名及语法高亮标记，
   使 Web 前端、API 适配器及测试断言能够 100% 字节级还原终端控制台的高保真着色效果。
"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class FileChange:
    """单文件物理变更提案模型。

    封装对知识库内单个 Markdown 文件执行变更所需的全部上下文元数据，
    包括操作类型、原始哈希（乐观锁依据）、前后内容快照及反向链接影响范围。
    采用不可变（frozen）设计，保证提案在审查与提交阶段的内容一致性。
    """

    operation: Literal["create", "update", "move", "trash", "frontmatter"]
    """文件变更操作类型枚举：
    - 'create': 新建全新笔记；
    - 'update': 覆盖或更新现有笔记全部正文；
    - 'move': 重命名或移动笔记文件；
    - 'trash': 安全软删除（移动至废纸篓）；
    - 'frontmatter': 仅修改笔记头部的 YAML Frontmatter 属性字典。
    """

    path: str
    """操作目标文件在知识库中的相对物理路径（例如 'Notes/AI.md'）。"""

    destination: str | None
    """移动或重命名操作的目标相对路径（仅当 operation == 'move' 时有值，其余操作为 None）。"""

    original_hash: str | None
    """变更前物理磁盘文件的 SHA-256 内容哈希（用于原子写入前的乐观并发冲突检测；新建文件时为 None）。"""

    original_content: str | None
    """变更前磁盘文件的原始文本快照（用于生成文本 Diff 差异及构建事务回滚日志；新建文件时为 None）。"""

    new_content: str | None
    """拟写入目标文件的新文本内容（删除操作 trash 时为 None）。"""

    affected_backlinks: tuple[str, ...] = ()
    """当执行移动或重命名时，全库中包含指向该笔记 WikiLink 的受影响外部笔记相对路径元组。"""


@dataclass(frozen=True)
class ChangeSet:
    """单文件变更集包装模型。

    用于 SafeWriteService 的原子操作输出封装，在领域概念上与多文件批量事务保持清晰边界。
    """

    file: FileChange
    """封装的单文件变更提案对象。"""


@dataclass(frozen=True)
class PreviewLine:
    """变更预览差异行模型（One line of a change preview）。

    携带在不同终端或客户端环境中精确复现终端高保真高亮所需的全部样式提示（Hints）。

    设计理念：
    `style` 为 Rich 样式名称（如 'green', 'red', 'bold yellow' 等）；
    `highlight` 对应 Rich 中 `Console.print(..., highlight=...)` 关键字参数。
    该参数绝非单纯的装饰：Rich 默认的高亮器会自动将 "Affected backlinks (2)" 中的数字进行加粗匹配，
    若非 Rich 适配器（例如 Web API 或快照测试断言）忽略了此标志，将输出肉眼可见的不同渲染结果。
    携带这两个字段允许跨平台客户端 100% 字节级还原终端控制台的渲染效果。
    """

    text: str
    """差异行的纯文本内容（如 Diff 标头、增删行或统计提示）。"""

    style: str | None = None
    """Rich 格式化样式名称（例如 'green', 'red', 'bold yellow', 'dim' 等）。"""

    highlight: bool = True
    """是否启用 Rich 默认的正规高亮器匹配（加粗数字、路径等）。"""

