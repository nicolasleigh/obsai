"""收件箱整理提案领域模型（Reviewable Inbox proposals）。

核心设计哲学：
1. 用户可审查性（Reviewable Proposals）：
   所有整理操作在执行前均以强类型、不可变提案（OrganizerProposal）的形式呈现，供 CLI 与前端交互界面展示、人工审查与批量勾选。
2. 保守安全决策（Conservative Decision Making）：
   当分类依据不足或存在目录冲突时，destination 为 None（或标记 issue），代表建议将笔记“保留在原位（leave in place）”，绝不盲目归档。
3. 语法安全性（WikiLink Sanitization）：
   自动过滤 WikiLink 标题中的控制字符、方括号与管道符，防止破坏 Markdown 语法树。
4. 默认高置信度门槛（Default Selection Threshold）：
   仅对置信度 >= 70% 且无任何冲突的提案默认预选中，最大限度降低 AI 自动操作的误判风险。
"""

from dataclasses import dataclass
import re
from pathlib import PurePosixPath


@dataclass(frozen=True)
class RelatedLink:
    """关联笔记双向链接建议模型。

    记录整理器推荐关联的相关笔记路径与显示标题，提供符合 Obsidian 规范的 WikiLink 渲染能力。
    """

    path: str
    """关联笔记在知识库中的相对路径（例如：Tech/Python/Concurrency.md）。"""

    title: str
    """关联笔记的显示标题或别名文本。"""

    @property
    def wikilink(self) -> str:
        """生成符合 Obsidian 语法的标准别名双链（`[[target|title]]`）。

        处理逻辑：
        1. 剥离末尾的 `.md` 后缀（Obsidian 惯例不包含扩展名）；
        2. 清洗标题中的 `[`、`]`、`|` 及换行符，防止破坏 Markdown AST；
        3. 若标题清洗后为空，保底回退使用目标路径的纯文件名。

        :return: 格式化后的 WikiLink 字符串
        """
        # 1. 规范化目标路径：Obsidian 双链内部通常去掉 .md 扩展名
        target = self.path[:-3] if self.path.lower().endswith(".md") else self.path
        # 2. 清洗标题中的特殊保留字符（方括号、管道符、回车换行），防止破坏双链语法解析
        title = re.sub(r"[\[\]|\r\n]", " ", self.title).strip() or PurePosixPath(target).name
        # 3. 组装为别名双链格式
        return f"[[{target}|{title}]]"


@dataclass(frozen=True)
class OrganizerProposal:
    """收件箱笔记整理提案模型。

    封装对单篇收件箱待整理笔记的完整重构建议，包括目标移动目录、标签补全、关联双链推荐及反向链接影响预估。
    采用不可变（frozen）设计，保证提议在通过审查与执行阶段的一致性。
    """

    path: str
    """待整理笔记在知识库中的原始相对路径（通常位于 Inbox/ 下）。"""

    destination: str | None
    """建议的目标存放路径（相对路径）。若为 None，表示建议保留在收件箱原位不移动。"""

    title: str
    """笔记标题（可能经过标准化处理）。"""

    add_tags: tuple[str, ...]
    """建议为笔记追加的新标签元组（保持不可变性）。"""

    add_links: tuple[RelatedLink, ...]
    """建议在笔记正文末尾追加的关联双链元组。"""

    affected_backlinks: tuple[str, ...]
    """移动该笔记后，因路径改变需要同步重写更新反向链接的外部笔记相对路径元组。"""

    confidence: float
    """算法分类与去向推断的置信度得分（范围：0.0 ~ 1.0）。"""

    reason: str
    """给出该整理建议的自然语言推理依据（如分类归因或建议保留原位的理由）。"""

    issue: str | None = None
    """阻碍提案执行的潜在冲突或错误原因（例如目标文件已存在）。None 表示无阻碍。"""

    @property
    def actionable(self) -> bool:
        """判断当前提案是否具备可执行性。

        必须同时满足：
        1. 指定了明确的目标移动路径（destination is not None）；
        2. 不存在任何阻塞性冲突或错误（issue is None）。
        """
        return self.destination is not None and self.issue is None

    @property
    def selected_by_default(self) -> bool:
        """判断当前提案在 CLI 或前端 UI 中是否应默认勾选。

        默认勾选规则：
        1. 必须是可执行提案（actionable 为 True）；
        2. 置信度得分必须达到 70% 及以上（confidence >= 0.70）。
        """
        return self.actionable and self.confidence >= 0.70
