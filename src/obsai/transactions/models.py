"""框架无关的纯领域事务数据契约模型（Plain, provider-independent transaction contracts）。

核心设计哲学与架构分层：
1. 意图与执行彻底解耦（Intent Decoupled from Execution）：
   系统通过 `TransactionOperation` 捕获用户或上层智能体（Agent）提出的高层抽象操作意图（如新建、局部替换、追加、重构双链等）；
   随后由事务服务编译为底层的物理变更方案，避免业务调用方直接操作底层文件指针或系统调用。
2. 零副作用全量预演方案（Dry-Run Execution Plan via TransactionPlan）：
   在对文件系统进行任何真实写操作前，事务引擎先在内存中推导出完全确定性的 `TransactionPlan`。
   记录了每份受影响文件的原始字节流快照（`originals`）、预期终态字节流（`finals`）、POSIX 权限模式（`original_modes`）、
   新创建的父目录树（`absent_directories`）以及被保守跳过的模糊双向链接（`ambiguous_backlinks`），
   为两阶段提交与预写日志持久化提供完备事实输入。
3. 严格的物理提交与索引补偿解耦（Two-Phase Durability via TransactionResult）：
   知识库物理文件落盘具有最高优先级。若物理写入成功但后续派生索引（SQLite / Vector）更新受阻，
   系统通过 `index_dirty=True` 显式标记，将主事务认定为 `committed=True` 并将索引脏状态交由后台补偿机制处理，
   坚决杜绝“因为衍生索引失败而强行回滚物理已生效文件”的反模式。
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from obsai.safe_write.models import FileChange


@dataclass(frozen=True)
class TransactionOperation:
    """高层抽象事务操作意图模型。

    用于表达对知识库发起的一项原子操作意图。采用不可变数据类设计，
    并提供标准工厂方法用于便捷构建具体的业务操作。
    """

    kind: Literal[
        "create", "replace", "append", "frontmatter", "move", "trash", "rewrite_backlinks"
    ]
    """操作意图类型：
    - 'create': 创建全新笔记；
    - 'replace': 精准替换现有笔记中的局部文本片段（要求在全文中唯一存在）；
    - 'append': 向现有笔记正文末尾追加内容；
    - 'frontmatter': 增量修改或修补 YAML Frontmatter 属性字典；
    - 'move': 重命名或移动笔记文件；
    - 'trash': 安全软删除（移动至废纸篓隔离区）；
    - 'rewrite_backlinks': 语法感知地级联改写外部笔记中的显式 WikiLink 反向链接。
    """

    path: str
    """操作目标文件在知识库中的相对物理路径（例如 'Work/Note.md'）。"""

    destination: str | None = None
    """移动或重命名时的目标相对路径（仅当 kind == 'move' 时有效）。"""

    old: str | None = None
    """待替换的旧文本片段（仅当 kind == 'replace' 时有效，且必须在全文中全局唯一出现）。"""

    new: str | None = None
    """拟写入、追加或替换的新文本正文（当 kind 为 'create', 'replace', 'append' 时有效）。"""

    updates: dict[str, Any] = field(default_factory=dict)
    """拟合并覆盖的元数据键值映射字典（仅当 kind == 'frontmatter' 时有效）。"""

    @classmethod
    def create(cls, path: str, content: str) -> "TransactionOperation":
        """构建创建全新笔记的操作意图。"""
        return cls("create", path, new=content)

    @classmethod
    def replace(cls, path: str, old: str, new: str) -> "TransactionOperation":
        """构建精准替换局部唯一文本片段的操作意图。"""
        return cls("replace", path, old=old, new=new)

    @classmethod
    def append(cls, path: str, content: str) -> "TransactionOperation":
        """构建向笔记正文末尾追加内容的操作意图。"""
        return cls("append", path, new=content)

    @classmethod
    def frontmatter(cls, path: str, updates: dict[str, Any]) -> "TransactionOperation":
        """构建增量修补 YAML Frontmatter 属性的操作意图。"""
        return cls("frontmatter", path, updates=updates)

    @classmethod
    def move(cls, path: str, destination: str) -> "TransactionOperation":
        """构建移动或重命名笔记的操作意图。"""
        return cls("move", path, destination=destination)

    @classmethod
    def trash(cls, path: str) -> "TransactionOperation":
        """构建将笔记移入废纸篓软删除的操作意图。"""
        return cls("trash", path)


@dataclass(frozen=True)
class TransactionPlan:
    """事务预演执行方案模型（Dry-Run Transaction Plan）。

    在物理变更发生前完全编译生成，封装整场事务所需的全部上下文、
    原始文件二进制快照、终态预期以及受影响的路径清单。
    """

    vault_root: str
    """目标 Vault 知识库在宿主机上的绝对物理根目录路径。"""

    operations: tuple[TransactionOperation, ...]
    """调用方最初提出的高层抽象操作意图元组。"""

    changes: tuple[FileChange, ...]
    """经过编译和反链推导生成的底层单文件物理变更提案元组。"""

    originals: dict[str, bytes | None]
    """受影响文件的原始二进制字节流内存快照字典（键为相对路径；新建文件对应值为 None）。
    注意：该字节流仅在内存中保留至预写日志快照物理落盘完毕，随后会被及时释放。
    """

    finals: dict[str, bytes | None]
    """事务成功后预期各目标文件的终态二进制字节流字典（删除文件对应值为 None）。"""

    original_modes: dict[str, int | None]
    """受影响文件的原始 POSIX 权限模式字典（st_mode & 0o777），用于回滚时精确复原权限。"""

    absent_directories: tuple[str, ...] = ()
    """在事务准备阶段尚不存在、由本事务按需新建的父目录路径元组（用于回滚时安全清理空目录）。"""

    ambiguous_backlinks: tuple[str, ...] = ()
    """在反向链接级联推导中，因仅匹配文件名短名（可能由 Obsidian 最短路径规则解析）而保守跳过改写的模糊引用路径元组。"""


@dataclass(frozen=True)
class TransactionResult:
    """事务应用结果模型。

    表征多文件事务在物理文件系统和衍生索引库中的最终落地状态。
    """

    transaction_id: str | None
    """执行事务的全局唯一标识符（若在准备前被直接拒绝则为 None）。"""

    committed: bool
    """物理文件系统的全部变更是否已成功持久化提交到磁盘。"""

    cancelled: bool = False
    """事务是否由于未经用户批准、冲突检测失败或外部取消而安全终止。"""

    index_dirty: bool = False
    """物理文件已成功提交，但后续 SQLite 衍生索引更新是否发生故障需要延迟补偿。"""

    index_error: str | None = None
    """当 index_dirty == True 时记录的索引更新异常摘要信息。"""
