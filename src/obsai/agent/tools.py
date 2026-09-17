"""Agent tool boundary. All Vault mutations pass through TransactionService.

该模块定义了 Agent 工具执行边界层。
将底层混合检索（Retriever）、知识图谱索引（IndexRepository）与安全事务服务（TransactionService）
统一封装为供 LangGraph 工作流调用的工具接口。

核心安全与架构设计：
1. 读写分离（CQRS）：只读工具直接执行；所有写操作绝不直接改动磁盘，必须经过两阶段提交（HITL 审核）。
2. 引用解耦（Reference-only）：工具产生的完整数据均落入 ArtifactStore，只向上层状态机返回轻量摘要与产物引用。
3. 链接完整性维护：移动笔记（move_note）时会自动触发反向链接（Backlinks）智能重写，保护知识图谱连通性。
"""

import base64
from dataclasses import asdict
from pathlib import Path
from typing import Any

from rich.console import Console

from obsai.agent.store import ArtifactStore
from obsai.retrieval.models import Retriever
from obsai.storage import Database, IndexRepository
from obsai.transactions import TransactionService
from obsai.transactions.models import TransactionOperation, TransactionPlan
from obsai.safe_write.models import FileChange

# 只读工具白名单：搜索笔记、读取正文、查询反向链接、查询出链
READ_TOOLS = frozenset({"search_notes", "read_note", "get_backlinks", "get_outgoing_links"})

# 写入工具白名单：新建笔记、局部替换、移动重命名、移入废纸篓、更新 YAML 前置元数据
WRITE_TOOLS = frozenset({"create_note", "update_note", "move_note", "trash_note", "update_frontmatter"})

# 所有可用工具全集
ALL_TOOLS = READ_TOOLS | WRITE_TOOLS


def _encode_bytes(values: dict[str, bytes | None]) -> dict[str, str | None]:
    """将包含二进制字节的字典通过 Base64 编码转换为 ASCII 字符串字典，以便 JSON 序列化。"""
    return {
        key: base64.b64encode(value).decode("ascii") if value is not None else None
        for key, value in values.items()
    }


def _decode_bytes(values: dict[str, str | None]) -> dict[str, bytes | None]:
    """将 Base64 编码的字符串字典还原为原始二进制 bytes 字典。"""
    return {
        key: base64.b64decode(value) if value is not None else None
        for key, value in values.items()
    }


def _serialize_plan(plan: TransactionPlan) -> dict[str, Any]:
    """将 TransactionPlan 事务计划对象序列化为可安全存储于 ArtifactStore 的纯 JSON 结构。"""
    return {
        "vault_root": plan.vault_root,
        "operations": [asdict(item) for item in plan.operations],
        "changes": [asdict(item) for item in plan.changes],
        "originals": _encode_bytes(plan.originals),
        "finals": _encode_bytes(plan.finals),
        "original_modes": plan.original_modes,
        "absent_directories": plan.absent_directories,
        "ambiguous_backlinks": plan.ambiguous_backlinks,
    }


def _deserialize_plan(value: dict[str, Any]) -> TransactionPlan:
    """从反序列化后的 JSON 字典重建强类型的 TransactionPlan 事务计划对象。"""
    return TransactionPlan(
        value["vault_root"],
        tuple(TransactionOperation(**item) for item in value["operations"]),
        tuple(
            FileChange(**{**item, "affected_backlinks": tuple(item["affected_backlinks"])})
            for item in value["changes"]
        ),
        _decode_bytes(value["originals"]),
        _decode_bytes(value["finals"]),
        value["original_modes"],
        tuple(value["absent_directories"]),
        tuple(value["ambiguous_backlinks"]),
    )


class AgentTools:
    """Agent 工具集门面类，负责只读工具的调度执行与写操作计划的生成和落库。"""

    def __init__(
        self,
        database_path: Path,
        vault_root: Path,
        retriever: Retriever,
        artifacts: ArtifactStore,
    ):
        """初始化工具边界服务。

        :param database_path: 笔记元数据与索引数据库路径（SQLite）
        :param vault_root: Obsidian 知识库真实文件系统根目录
        :param retriever: 混合检索器实例（全文检索 + 语义向量检索）
        :param artifacts: 产物持久化存储库（用于暂存大体量结果与事务计划）
        """
        self.database_path = database_path
        self.vault_root = vault_root
        self.retriever = retriever
        self.artifacts = artifacts

    def read(self, name: str, args: dict[str, Any]) -> tuple[str, list[str], list[str], str]:
        """执行只读类工具。

        全量结果写入 ArtifactStore，仅向上层返回轻量摘要和引用。

        :param name: 工具名称（必须属于 READ_TOOLS）
        :param args: 工具调用参数字典
        :return: (产物引用 key, 涉及笔记 ID 列表, 涉及分块 ID 列表, 紧凑单行文本摘要)
        """
        if name not in READ_TOOLS:
            raise ValueError(f"Unknown read tool: {name}")

        # 工具 1：笔记检索
        if name == "search_notes":
            results = self.retriever.search(
                str(args["query"]), limit=min(int(args.get("limit", 10)), 20)
            )
            payload = [result.model_dump(mode="json") for result in results]
            ref = self.artifacts.put(payload)
            # 生成前 5 项的简短单行摘要，避免大体积文本污染模型后续上下文
            summary = "; ".join(
                f"note_id={item.note_id} chunk_id={item.chunk_id} {item.title} ({item.path})"
                for item in results[:5]
            ) or "No results"
            return ref, [item.note_id for item in results], [item.chunk_id for item in results], summary

        # 工具 2/3/4：读取单篇笔记、查询反向链接、查询出链（依赖索引库）
        with Database(self.database_path) as database:
            repository = IndexRepository(database)
            note_id = str(args["note_id"])
            note = repository.notes.get(note_id)
            if note is None:
                raise KeyError(f"Unknown note ID: {note_id}")

            if name == "read_note":
                parsed = repository.notes.get_parsed(note_id)
                payload = parsed.model_dump(mode="json") if parsed is not None else {}
                # 仅截取前 500 字符作为快速摘要
                summary = f"Read {note.path}: {str(payload.get('raw_content', ''))[:500]}"
            elif name == "get_backlinks":
                payload = [asdict(item) for item in repository.backlinks_for_path(note_id, note.path)]
                summary = f"{len(payload)} backlinks to {note.path}"
            else:  # get_outgoing_links
                payload = [asdict(item) for item in repository.links_for_note(note_id)]
                summary = f"{len(payload)} outgoing links from {note.path}"

            return self.artifacts.put(payload), [note_id], [], summary

    def plan_write(self, name: str, args: dict[str, Any]) -> tuple[str, str]:
        """生成写操作提案（两阶段提交的第一阶段）。

        计算文件变更、受影响反链并生成 Diff 预览，但绝不直接修改磁盘文件。

        :param name: 写工具名称（必须属于 WRITE_TOOLS）
        :param args: 写工具调用参数字典
        :return: (已暂存计划的产物引用 key, 格式化的终端 Diff 差异预览文本)
        """
        if name not in WRITE_TOOLS:
            raise ValueError(f"Unknown write tool: {name}")

        service = TransactionService(self.vault_root, database_path=self.database_path)
        path = str(args["path"])

        # 根据不同写操作构建对应的事务原子操作
        if name == "create_note":
            plan = service.plan([TransactionOperation.create(path, str(args["content"]))])
        elif name == "update_note":
            plan = service.plan([TransactionOperation.replace(path, str(args["old"]), str(args["new"]))])
        elif name == "move_note":
            # 移动笔记时，自动扫描并智能重写全库反向链接（WikiLinks / Markdown links）
            plan = service.plan_move_with_backlinks(path, str(args["destination"]))
        elif name == "trash_note":
            plan = service.plan([TransactionOperation.trash(path)])
        else:  # update_frontmatter
            updates = args["updates"]
            if not isinstance(updates, dict):
                raise ValueError("updates must be a mapping")
            plan = service.plan([TransactionOperation.frontmatter(path, updates)])

        # 使用 Rich 捕获差异渲染文本，供前端/CLI 展示给用户进行人机审核
        capture = Console(record=True, force_terminal=False, width=100)
        service.preview(plan, capture)
        preview = capture.export_text()

        # 将二进制安全序列化后的计划存入产物库
        ref = self.artifacts.put(_serialize_plan(plan))
        return ref, preview

    def apply_write(self, ref: str):
        """执行已获批准的写操作计划（两阶段提交的第二阶段）。

        :param ref: 暂存事务计划的产物引用 key
        :return: 事务执行结果（包含写入成功的文件列表及应用状态）
        """
        service = TransactionService(self.vault_root, database_path=self.database_path)
        plan = _deserialize_plan(self.artifacts.get(ref))
        # 经人工审批确认后，安全原子执行磁盘写入并保持回滚保障
        return service.execute(plan, approved=True)
