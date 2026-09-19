"""收件箱笔记智能整理服务（Conservative local Inbox classifier and one-approval/one-transaction planner）。

核心设计哲学与安全架构：
1. 本地语义检索与共现目录推断（Local Retrieval & Co-occurrence Directory Clustering）：
   基于待整理笔记的标题、标签及章节标题提取核心语义关键词，检索知识库中已归档的相似笔记；
   按父目录对命中结果进行加权聚合打分，自动推导最合适的归档目的地。
2. 保守决策与歧义规避（Conservative Heuristic & Ambiguity Avoidance）：
   - 绝对优势原则：排名第一的目录得分必须超过第二名至少 25%（1.25x），否则视为歧义，判定为“保留在收件箱原位”；
   - 弱相关人工确认：单关键词匹配的基础置信度为 0.65，严格低于 0.70 默认勾选阈值，防止自动化盲目操作。
3. 批量目标同名冲突熔断（Cross-Proposal Collision Detection）：
   在生成提案阶段，全局校验所有收件箱笔记的目标路径；若发现两篇不同笔记被提议移动至完全相同的目标路径，
   立即标记 issue 阻断执行，防止文件相互覆盖。
4. 全库反向链接级联更新与两阶段原子提交（2PC Transaction & Backlink Rewrite）：
   依托 TransactionService，将选中的所有移动操作、Frontmatter 标签去重合并、正文尾部推荐双链追加，
   以及全 Vault 引用者笔记的反向链接重写合并为单个原子事务计划，具备完整的 WAL 日志与断电回滚保护。
"""

import re
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Sequence

from obsai.errors import CollisionError, ConfigError, TransactionError
from obsai.organizer.models import OrganizerProposal, RelatedLink
from obsai.retrieval.models import Retriever
from obsai.safe_write.service import SafeWriteService
from obsai.storage.repositories import IndexRepository
from obsai.transactions import TransactionService
from obsai.transactions.models import TransactionOperation, TransactionPlan, TransactionResult
from obsai.vault.parser import parse_note
from obsai.vault.scanner import scan_markdown_files
from obsai.shutdown import check_shutdown


def _terms(note) -> list[str]:
    """从笔记元数据中提取用于语义检索的核心关键词条。

    提取来源：
    1. 笔记标题（note.title）；
    2. 笔记已有标签（note.tags）；
    3. 笔记正文前 2 个章节标题（note.headings[:2]）。

    :param note: 已解析的 Note 对象
    :return: 经过清洗、去重与长度过滤的关键词列表（最多 8 个）
    """
    # 汇集标题、标签以及前两个各级标题文本
    values = [note.title, *note.tags, *(heading.text for heading in note.headings[:2])]
    terms = []
    for value in values:
        # 使用正则提取 Unicode 字母数字词（自动支持多国语言并过滤标点符号）
        for word in re.findall(r"[^\W_]+", value.lower(), re.UNICODE):
            # 过滤条件：英文单词长度 >= 3，或者包含 CJK 中日韩汉字字符（\u3400 ~ \u9fff）
            if len(word) >= 3 or any("\u3400" <= char <= "\u9fff" for char in word):
                # 保持先后顺序去重
                if word not in terms:
                    terms.append(word)
    # 最多保留前 8 个高优先级词条，降低检索与模型开销
    return terms[:8]


def _filename(note) -> str:
    """根据笔记标题规范化生成目标文件名。

    处理逻辑：
    1. 若现有文件名（不含扩展名的 stem）已经等于标题，直接保持原文件名；
    2. 过滤操作系统文件路径保留字符，并去除首尾空白与点号；
    3. 若清洗后为空、非法保留名（'.' 或 '..'）或超长（> 80 字符），安全降级回退使用原文件名；
    4. 否则返回 '{title}.md'。

    :param note: 已解析的 Note 对象
    :return: 合法且安全的 Markdown 文件名
    """
    stem = PurePosixPath(note.path).stem
    # 若标题已与当前文件名一致，直接复用既有名称
    if note.title == stem:
        return PurePosixPath(note.path).name
    # 替换各类操作系统非法文件字符（反斜杠、斜杠、控制字符、冒号、星号、问号、双引号、尖括号、管道符）为横线 '-'
    title = re.sub(r"[\\/\x00-\x1f:*?\"<>|]", "-", note.title).strip(" .")
    # 异常或超长名称回退至原文件名
    if not title or title in {".", ".."} or len(title) > 80:
        return PurePosixPath(note.path).name
    return f"{title}.md"


class InboxOrganizer:
    """收件箱笔记智能整理服务。

    负责收件箱笔记的启发式归类、目标路径预测、标签与双链关联推荐，并生成全库原子事务计划。
    """

    def __init__(self, vault_root: Path, database_path: Path, repository: IndexRepository,
                 retriever: Retriever, *, inbox: str = "Inbox"):
        """初始化收件箱整理器。

        :param vault_root: Obsidian 知识库物理根目录
        :param database_path: 索引数据库路径
        :param repository: 索引元数据仓储
        :param retriever: 混合检索器（用于相关笔记与语义检索）
        :param inbox: 收件箱相对路径（默认 'Inbox'）
        """
        self.vault = vault_root.expanduser().resolve(strict=True)
        self.database_path = database_path
        self.repository = repository
        self.retriever = retriever
        self.safe = SafeWriteService(self.vault)
        # 路径安全检查：去除末尾斜杠，并通过 SafeWriteService 进行沙箱与软链接合法性探针校验
        self.inbox = inbox.rstrip("/")
        self.safe.path(f"{self.inbox}/probe.md")
        self.transaction = TransactionService(self.vault, database_path=database_path)

    def propose(self) -> list[OrganizerProposal]:
        """扫描收件箱，为其中所有笔记生成整理提案（只读分析，无文件系统写副作用）。

        执行流程：
        1. 检查事务日志就绪性（确保无挂起的未提交事务）；
        2. 校验收件箱目录物理有效性（拒绝软链接）；
        3. 扫描全库文件，提取位于收件箱内的待整理笔记；
        4. 逐篇进行单文件推断（_propose_one）；
        5. 全局碰撞检测：若多篇收件箱笔记被建议移至相同目标路径，统一打上 issue 标记阻断自动执行。

        :return: 所有待整理笔记的提案列表
        :raises ConfigError: 收件箱不存在或为非法符号链接时抛出
        """
        # 1. 事务就绪性检查
        self.transaction.ensure_ready()
        inbox_root = self.vault / self.inbox
        # 2. 验证收件箱物理目录
        if not inbox_root.is_dir() or inbox_root.is_symlink():
            raise ConfigError(f"Inbox directory does not exist: {self.inbox}")
        # 3. 扫描知识库，提取收件箱内的待处理 Markdown 笔记
        scanned = scan_markdown_files(self.vault)
        paths = [path for path in scanned
                 if path.relative_to(self.vault).as_posix().startswith(self.inbox + "/")]
        visible_paths = {path.relative_to(self.vault).as_posix()
                         for path in scanned}
        # 4. 逐篇分析并生成提案
        proposals = [self._propose_one(parse_note(path, vault_root=self.vault), visible_paths)
                     for path in paths]
        # 5. 跨提案同名目标冲突熔断检测
        destinations = defaultdict(list)
        for index, proposal in enumerate(proposals):
            if proposal.destination:
                destinations[proposal.destination].append(index)
        for indices in destinations.values():
            if len(indices) > 1:
                for index in indices:
                    proposal = proposals[index]
                    # 发现多个收件箱笔记冲突时，将其重写为包含 issue 的不可执行提案
                    proposals[index] = OrganizerProposal(
                        proposal.path, proposal.destination, proposal.title, proposal.add_tags,
                        proposal.add_links, proposal.affected_backlinks, proposal.confidence,
                        proposal.reason, "Multiple Inbox notes propose the same destination",
                    )
        return proposals

    def _propose_one(self, note, visible_paths: set[str]) -> OrganizerProposal:
        """为单篇收件箱笔记推导归类目标、关联双链与标签推荐。

        推导策略：
        1. 提取核心关键词并在库内检索相关已归档笔记；
        2. 聚合各笔记命中频次，计算各目标父目录的总分；
        3. 保守性检验：若无命中或第一名未达到第二名得分的 1.25 倍，判定为歧义，保留在原位；
        4. 提取胜出目录下最相关的 2 篇笔记，推荐生成 WikiLink 与继承标签；
        5. 调用 SafeWrite 进行移动干运行（Dry-run），探测反向链接影响与同名覆盖冲突；
        6. 根据得分计算平滑置信度（单关键词最高 0.65，不触发默认选中）。

        :param note: 待整理的 Note 对象
        :param visible_paths: 全库有效笔记路径集合
        :return: 封装好的 OrganizerProposal 对象
        """
        check_shutdown()
        evidence: dict[str, float] = defaultdict(float)
        # 1. 关键词检索与证据积累
        for term in _terms(note):
            check_shutdown()
            try:
                results = self.retriever.search(term, limit=20)
            except Exception as exc:
                # 检索器异常时安全降级，不中断整体流程
                return OrganizerProposal(note.path, None, note.title, (), (), (), 0.0,
                                         f"Related-note retrieval failed: {type(exc).__name__}: {exc}")
            seen_for_term: set[str] = set()
            for result in results:
                # 排除当前词已计入的笔记，并排除收件箱内部的未整理笔记
                if result.note_id in seen_for_term or result.path.startswith(self.inbox + "/"):
                    continue
                seen_for_term.add(result.note_id)
                # 必须为物理可见笔记
                if result.path not in visible_paths:
                    continue
                # 排除根目录笔记，且父目录必须真实存在
                parent = PurePosixPath(result.path).parent
                if str(parent) == "." or not (self.vault / str(parent)).is_dir():
                    continue
                evidence[result.note_id] += 1.0

        # 2. 目录级权重聚类统计
        directory_scores: dict[str, float] = defaultdict(float)
        candidate_notes = {}
        for note_id, score in evidence.items():
            record = self.repository.notes.get(note_id)
            if record is None:
                continue
            directory = str(PurePosixPath(record.path).parent)
            if directory == self.inbox or directory.startswith(self.inbox + "/"):
                continue
            directory_scores[directory] += score
            candidate_notes[note_id] = (record, score)

        # 3. 候选目录排序与绝对优势对决
        ranked = sorted(directory_scores.items(), key=lambda item: (-item[1], item[0]))
        # 保守准则：第一名得分必须超出第二名至少 25%（1.25x），否则视为歧义留存收件箱
        if not ranked or (len(ranked) > 1 and ranked[0][1] <= ranked[1][1] * 1.25):
            reason = "No related indexed notes in a clear existing directory" if not ranked else "Related notes point to multiple directories"
            return OrganizerProposal(note.path, None, note.title, (), (), (), 0.0, reason)

        # 4. 确定胜出目录并生成目标文件名
        directory, score = ranked[0]
        destination = f"{directory}/{_filename(note)}"
        # 提取该目录下评分最高的相关笔记
        related = sorted(
            ((record, value) for record, value in candidate_notes.values()
             if str(PurePosixPath(record.path).parent) == directory),
            key=lambda item: (-item[1], item[0].path),
        )
        # 提取前 2 篇相关笔记构建推荐双链（过滤已存链接与非法字符）
        existing_targets = {link.target_path for link in note.wikilinks}
        links = tuple(
            RelatedLink(record.path, record.title) for record, _ in related[:2]
            if record.path[:-3] not in existing_targets and record.path not in existing_targets
            and not any(char in record.path for char in "[]|\r\n")
        )
        # 借用相关笔记的标签（去重并排除已有标签）
        tags = []
        for record, _ in related[:2]:
            for tag in self.repository.tags_for_note(record.id):
                if tag not in note.tags and tag not in tags:
                    tags.append(tag)

        # 5. 移动干运行（Dry-run）与反链影响探测
        issue = None
        impacts: tuple[str, ...] = ()
        try:
            impacts = self.safe.move_note(note.path, destination).file.affected_backlinks
        except CollisionError:
            issue = "Destination already exists"

        # 6. 置信度打分：基础分 0.55，每点证据 +0.10，最高 0.95
        # 单关键词命中得分 1.0 时置信度为 0.65，故意低于 0.70 默认自动勾选门槛
        confidence = min(0.95, 0.55 + 0.10 * score)
        reason = f"Matched {len(related)} indexed note(s) in existing directory {directory}"
        return OrganizerProposal(note.path, destination, note.title, tuple(tags[:5]), links,
                                 impacts, confidence, reason, issue)

    def plan(self, proposals: Sequence[OrganizerProposal], selected: Sequence[int]) -> TransactionPlan:
        """将用户选中的提案编排为单个原子事务计划（包含反向链接级联更新）。

        执行流程：
        1. 校验选中的 1-based 序号合法性、唯一性与提案可执行性；
        2. 收集所有文件移动操作；
        3. 对每个选中笔记生成 Frontmatter 标签合并与正文末尾双链追加操作；
        4. 调用事务引擎的 plan_moves_with_backlinks，自动级联计算全库反链重写操作并打包。

        :param proposals: propose() 返回的完整提案列表
        :param selected: 用户勾选的提案序号序列（1-based，例如 [1, 3]）
        :return: 包含所有移动、内容修改与反链更新的 TransactionPlan
        :raises TransactionError: 序号无效或提案不可执行时抛出
        """
        if not selected or len(set(selected)) != len(selected):
            raise TransactionError("Select at least one distinct proposal")
        chosen = []
        for index in selected:
            check_shutdown()
            if index < 1 or index > len(proposals):
                raise TransactionError(f"Invalid proposal number: {index}")
            proposal = proposals[index - 1]
            if not proposal.actionable:
                raise TransactionError(f"Proposal {index} has no safe destination: {proposal.issue or proposal.reason}")
            chosen.append(proposal)

        moves = [(proposal.path, proposal.destination) for proposal in chosen]
        extras: list[TransactionOperation] = []
        for proposal in chosen:
            check_shutdown()
            assert proposal.destination is not None
            parsed = parse_note(self.vault / proposal.path, vault_root=self.vault)
            # 处理 Frontmatter 标签合并操作
            if proposal.add_tags:
                current = parsed.frontmatter.get("tags", [])
                if not isinstance(current, (str, list)):
                    raise TransactionError(f"Unsupported existing tags format: {proposal.path}")
                current_tags = [current] if isinstance(current, str) else list(current)
                if any(not isinstance(tag, str) for tag in current_tags):
                    raise TransactionError(f"Unsupported existing tags format: {proposal.path}")
                extras.append(TransactionOperation.frontmatter(
                    proposal.destination,
                    {"tags": list(dict.fromkeys([*current_tags, *proposal.add_tags]))},
                ))
            # 处理正文尾部推荐双链追加操作
            if proposal.add_links:
                suffix = "\n\nRelated:\n" + "".join(f"- {link.wikilink}\n" for link in proposal.add_links)
                extras.append(TransactionOperation.append(proposal.destination, suffix))

        # 核心：自动扫描全库受影响反链，生成全库同步重写与文件移动绑定的统一原子事务计划
        return self.transaction.plan_moves_with_backlinks(moves, extras)

    def apply(self, plan: TransactionPlan) -> TransactionResult:
        """执行已获批准的事务计划。

        :param plan: 经 plan() 编排生成的原子事务计划
        :return: 事务执行结果（包含变更统计与反链更新明细）
        """
        return self.transaction.execute(plan, approved=True)

