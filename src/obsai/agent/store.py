"""Separate, durable tool artifacts. Checkpoints contain opaque refs only.

该模块提供独立的、持久化的 Agent 工具产物存储库（ArtifactStore）。
基于 SQLite 实现轻量级 Key-Value 存储，遵循“仅存引用（Reference-only）”的设计模式：
将大体量的检索结果、笔记正文及写操作提案参数存入产物库，而在 LangGraph 的 Checkpoint
状态中仅保留 32 位不透明引用（UUID hex），以此保障状态机持久化的高效与轻量。
"""

import json
import sqlite3
from pathlib import Path
from uuid import uuid4


class ArtifactStore:
    """基于 SQLite 的 Agent 产物键值持久化存储。"""

    def __init__(self, path: Path):
        """初始化产物库连接，并确保数据表已创建。

        :param path: SQLite 数据库文件路径（通常为 *.agent-artifacts.db）
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        # 极简键值表：id 为 32 位 UUID 引用标识，payload 为 JSON 序列化数据
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self.connection.commit()

    def put(self, value: object) -> str:
        """持久化存储一个产物对象，并返回其全局唯一的引用标识。

        :param value: 可被 JSON 序列化的数据对象（如检索结果列表、提案参数等）
        :return: 32 位十六进制 UUID 引用字符串（ref）
        """
        ref = uuid4().hex
        self.connection.execute(
            "INSERT INTO artifacts VALUES (?, ?)",
            (ref, json.dumps(value, ensure_ascii=False, sort_keys=True)),
        )
        self.connection.commit()
        return ref

    def get(self, ref: str) -> object:
        """根据产物引用标识查询并还原数据对象。

        :param ref: 产物的 32 位 UUID 引用标识
        :return: 反序列化还原后的 Python 原生数据对象
        :raises KeyError: 当指定的产物引用不存在时抛出
        """
        row = self.connection.execute(
            "SELECT payload FROM artifacts WHERE id = ?", (ref,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Missing agent artifact: {ref}")
        return json.loads(row[0])

    def close(self) -> None:
        """关闭 SQLite 数据库连接，释放文件描述符与锁。"""
        self.connection.close()
