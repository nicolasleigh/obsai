"""安全写入子系统核心业务服务（Previewed, OCC-checked, single-note Vault writes）。

核心设计哲学与安全架构：
1. 不可变预演提案机制（Immutable Dry-Run Proposals）：
   任何写操作在落盘前，必须先生成强类型、不可变变更模型（FileChange / ChangeSet），
   计算结构化 Diff 差异及全库反向链接影响范围，用于人工审查或转交事务引擎。
2. 乐观并发控制与防脏写（OCC / CAS via original_hash）：
   读取并记录原始内容的 SHA-256 哈希值作为版本凭证；在物理落盘前的临门一脚再次重算哈希比对，
   若笔记在审查期间已被外部编辑器（如 Obsidian、Git 等）修改，立即阻断写入，彻底避免并发脏写覆盖。
3. 严格沙箱与符号链接防御（Path Hardening & Symlink Resistance）：
   - 拒绝跨目录逃逸（..、绝对路径）；
   - 白名单限定仅允许修改 .md 扩展名笔记；
   - 内部专有目录隔离保护（.obsai-trash, .obsai-transactions）；
   - 逐级步进检测符号链接（Symlink），坚决防御指向知识库外部任意文件的软链接提权攻击。
4. 工业级原子落盘与持久性保证（Atomic Durability & Crash-Safety）：
   - 在目标父目录就近生成临时文件（确保在同物理分区，支持原子系统调用）；
   - 克隆原文件 POSIX 权限位；
   - 双重 fsync（先刷盘数据文件，再刷盘父目录项 dentry）；
   - 针对新建使用 os.link 实现无覆盖风险的原子占位；针对更新使用 os.replace 实现无损原子替换；
   - 移动与软删除操作采用硬链接就位 + 解除原链接，若 unlink 失败则自动补偿回滚，杜绝双份数据或数据丢失。
5. 人工确认守卫（Mandatory Approval Guard）：
   未经上层或用户显式授予 approved=True，apply 方法绝对不产生任何物理磁盘副作用。
"""

import difflib
import hashlib
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath
from uuid import uuid4
from typing import Any, Mapping

import yaml
from rich.console import Console

from obsai.errors import CollisionError, ConflictError, InvalidEncodingError, SafeWriteError, VaultError
from obsai.safe_write.models import ChangeSet, FileChange, PreviewLine
from obsai.vault.parser import parse_note
from obsai.vault.scanner import scan_markdown_files


def _hash(data: bytes) -> str:
    """计算二进制字节流的标准 64 位十六进制 SHA-256 哈希字符串。

    作为乐观并发控制（OCC / CAS）判定文件是否发生外部并发修改的唯一指纹依据。
    """
    return hashlib.sha256(data).hexdigest()


def _encode(value: str) -> bytes:
    """以严格模式将 Unicode 字符串编码为 UTF-8 字节流。

    Raises:
        InvalidEncodingError: 当文本中包含无法按 UTF-8 编码的非法字符时抛出，阻断脏数据入库。
    """
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise InvalidEncodingError("New note content is not valid UTF-8") from exc


def _sync_directory(path: Path) -> None:
    """强制持久化刷盘父目录项元数据（Best-effort directory durability）。

    在 POSIX 文件系统中，link、replace、unlink 等操作修改的是目录项（dentry）。
    为保证物理断电时的崩溃一致性，需对目录文件描述符执行 fsync。
    对于不支持目录 fsync 的系统环境（如某些 Windows/网络共享驱动器），进行安全静默降级。
    """
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def patch_frontmatter(content: str, updates: Mapping[str, Any]) -> str:
    """精准修补 Markdown 笔记的 YAML Frontmatter 元数据头部，百分之百逐字保留正文。

    特性：
    - 若原笔记已存在 Frontmatter（首行为 '---' 并以 '---' 或 '...' 闭合），解析出字典并与 updates 合并；
    - 若原笔记无 Frontmatter，自动在正文顶部新建 Frontmatter 块；
    - 逐字保留闭合标识之后的所有正文内容（包括空行、缩进及换行风格）；
    - 使用 safe_dump 重新序列化，保留 Unicode 字符（allow_unicode=True），并保持键相对顺序。

    Raises:
        SafeWriteError: 当 updates 键不合法、YAML 结构损坏、或无法序列化时抛出。
    """
    if not updates or any(not isinstance(key, str) or not key for key in updates):
        raise SafeWriteError("Frontmatter updates require nonempty string keys")
    lines = content.splitlines(keepends=True)
    body = content
    metadata: dict[str, Any] = {}
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), None)
        if end is None:
            raise SafeWriteError("Unclosed YAML frontmatter")
        try:
            loaded = yaml.safe_load("".join(lines[1:end]))
        except yaml.YAMLError as exc:
            raise SafeWriteError(f"Invalid YAML frontmatter: {exc}") from exc
        if loaded is not None:
            if not isinstance(loaded, dict) or any(not isinstance(key, str) for key in loaded):
                raise SafeWriteError("Frontmatter must be a mapping with string keys")
            metadata = loaded
        body = "".join(lines[end + 1 :])
    metadata.update(updates)
    try:
        header = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
    except yaml.YAMLError as exc:
        raise SafeWriteError(f"Cannot serialize frontmatter: {exc}") from exc
    updated = f"---\n{header}---\n{body}"
    _encode(updated)
    return updated


def change_preview_lines(change: ChangeSet) -> list[PreviewLine]:
    """为单文件变更集生成结构化差异行列表，不直接执行终端渲染输出。

    设计理念：
    将 Diff 计算与具体的渲染器（Renderer）彻底解耦，为应用层、Web API 适配器
    以及测试断言提供统一的结构化数据，同时终端命令行的 `preview` 保持完全一致的视觉呈现。
    """
    file = change.file
    old_label = f"a/{file.path}" if file.original_content is not None else "/dev/null"
    new_label = (
        f"b/{file.destination or file.path}" if file.operation != "trash" else "/dev/null"
    )
    if file.operation == "move":
        lines = [f"--- {old_label}\n", f"+++ {new_label}\n", "(content unchanged; path moves)\n"]
    else:
        lines = list(difflib.unified_diff(
            (file.original_content or "").splitlines(keepends=True),
            (file.new_content or "").splitlines(keepends=True),
            fromfile=old_label, tofile=new_label,
        ))
    if not lines:
        lines = [f"--- {old_label}\n", f"+++ {new_label}\n", "(empty content)\n"]
    rendered: list[PreviewLine] = []
    for line in lines:
        style = "green" if line.startswith("+") else "red" if line.startswith("-") else "cyan" if line.startswith("@@") else None
        rendered.append(PreviewLine(line.rstrip("\n"), style, highlight=False))
    if file.operation == "trash":
        rendered.append(PreviewLine(f"Trash destination: {file.destination}", "yellow"))
    if file.affected_backlinks:
        rendered.append(PreviewLine(
            f"Affected backlinks ({len(file.affected_backlinks)}); they will not be rewritten:",
            "yellow",
        ))
        rendered.extend(PreviewLine(f"  {source}") for source in file.affected_backlinks)
    return rendered


class SafeWriteService:
    """安全写入服务。

    提供对 Obsidian 知识库单文件进行变更提案生成（Create/Update/Move/Trash/Frontmatter）、
    差异对比预览（Preview）、以及原子安全落地（Apply）的全生命周期管理。
    """

    def __init__(self, vault_root: Path):
        """初始化安全写入服务。

        Args:
            vault_root: 目标知识库的根目录路径。

        Raises:
            VaultError: 当知识库根目录不存在、无法访问或不是物理目录时抛出。
        """
        try:
            self.root = vault_root.expanduser().resolve(strict=True)
        except OSError as exc:
            raise VaultError(f"Cannot access vault {vault_root}: {exc}") from exc
        if not self.root.is_dir():
            raise VaultError(f"Vault is not a directory: {vault_root}")

    def path(self, relative: str, *, internal: bool = False) -> Path:
        """将知识库相对 POSIX 路径解析为安全可信的物理绝对路径，拒绝一切越界与不安全操作。

        作为系统级安全守卫（Security Gatekeeper），执行以下严苛防护：
        1. 基础合法性校验：拒绝空路径、反斜杠 `\\` 及空字符 `\\x00` 截断攻击；
        2. 目录逃逸拦截：使用 PurePosixPath 校验，拒绝绝对路径及包含 `.` 或 `..` 的逃逸路径；
        3. 扩展名白名单：仅允许修改 `.md` Markdown 笔记；
        4. 保留内部目录隔离：禁止非 internal 调用访问 `.obsai-trash` 或 `.obsai-transactions`；
        5. 逐级符号链接防御（Symlink Traversal Check）：自根向下步进探测，若路径中任一部分为软链接，立即阻断，杜绝软链提权；
        6. 最终归属校验：真实解析后必须严格位于 `self.root` 之内（is_relative_to）。

        Args:
            relative: 相对知识库根目录的 POSIX 相对路径字符串（如 'Folder/Note.md'）。
            internal: 是否允许访问系统内部保留目录（如废纸篓或事务日志目录）。

        Returns:
            经过安全验证的绝对物理 Path 对象。

        Raises:
            SafeWriteError: 当路径违反上述任意安全规则时抛出。
        """
        if not relative or "\\" in relative or "\x00" in relative:
            raise SafeWriteError("Use a nonempty Vault-relative POSIX Markdown path")
        candidate = PurePosixPath(relative)
        if candidate.is_absolute() or any(part in (".", "..") for part in relative.split("/")):
            raise SafeWriteError(f"Path escapes or is not relative to the Vault: {relative}")
        if candidate.suffix.lower() != ".md":
            raise SafeWriteError("Only Markdown .md notes may be changed")
        if not internal and candidate.parts[0] in (".obsai-trash", ".obsai-transactions"):
            raise SafeWriteError("The Vault internal directory is reserved")
        path = self.root.joinpath(*candidate.parts)
        cursor = self.root
        for part in candidate.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise SafeWriteError(f"Symlinked Vault paths are not writable: {relative}")
        if not path.resolve(strict=False).is_relative_to(self.root):
            raise SafeWriteError(f"Path escapes the Vault: {relative}")
        return path

    def _read(self, path: Path) -> tuple[str, str]:
        """安全读取常规笔记文件内容，并计算其当前 SHA-256 哈希值。

        Returns:
            (解码后的 UTF-8 文本内容, 64位十六进制 SHA-256 哈希)。

        Raises:
            ConflictError: 目标笔记在读取瞬间已从磁盘消失；
            SafeWriteError: 目标不是常规文件或底层 I/O 访问受阻；
            InvalidEncodingError: 笔记内容不符合严格 UTF-8 规范。
        """
        try:
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise SafeWriteError(f"Not a regular note: {path.relative_to(self.root)}")
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise ConflictError(f"Note disappeared: {path.relative_to(self.root)}") from exc
        except OSError as exc:
            raise SafeWriteError(f"Cannot read note {path}: {exc}") from exc
        try:
            return data.decode("utf-8", errors="strict"), _hash(data)
        except UnicodeError as exc:
            raise InvalidEncodingError(f"Note is not UTF-8: {path.relative_to(self.root)}") from exc

    def _vacant(self, path: Path) -> None:
        """验证目标路径处于空置状态（不存在文件或符号链接），且父目录合法。

        Raises:
            CollisionError: 目标路径已存在同名文件或符号链接（写踩踏冲突）；
            SafeWriteError: 目标父路径已存在但不是目录。
        """
        if path.exists() or path.is_symlink():
            raise CollisionError(f"Destination already exists: {path.relative_to(self.root)}")
        if path.parent.exists() and not path.parent.is_dir():
            raise SafeWriteError(f"Destination parent is not a directory: {path.parent}")

    def create_note(self, path: str, content: str) -> ChangeSet:
        """生成创建全新笔记的变更提案（Dry-Run 无副作用）。

        Args:
            path: 待创建笔记的相对路径。
            content: 拟写入的正文内容。

        Returns:
            封装好的不可变 ChangeSet 提案。
        """
        target = self.path(path)
        self._vacant(target)
        _encode(content)
        return ChangeSet(FileChange("create", path, None, None, None, content))

    def update_note(self, path: str, old: str, replacement: str) -> ChangeSet:
        """生成精确替换笔记局部文本片段的变更提案（Dry-Run 无副作用）。

        核心防灾设计：
        拒绝大模型或客户端进行全篇盲目黑盒覆盖，要求待替换的旧文本片段在笔记中
        必须且仅能出现一次（count == 1），确保替换操作完全无歧义。

        Args:
            path: 目标笔记的相对路径。
            old: 待替换的旧文本子串（必须在文档中全局唯一存在）。
            replacement: 拟替换的新文本子串。

        Returns:
            封装好的不可变 ChangeSet 提案（携带当前文件的 original_hash 用于 CAS 检验）。

        Raises:
            SafeWriteError: 当旧文本未在全文中精确出现 1 次，或替换后内容未发生改变时抛出。
        """
        target = self.path(path)
        content, original_hash = self._read(target)
        if not old or content.count(old) != 1:
            raise SafeWriteError("The old text must occur exactly once")
        updated = content.replace(old, replacement, 1)
        if updated == content:
            raise SafeWriteError("Replacement does not change the note")
        _encode(updated)
        return ChangeSet(FileChange("update", path, None, original_hash, content, updated))

    def update_frontmatter(self, path: str, updates: Mapping[str, Any]) -> ChangeSet:
        """生成局部修补 Frontmatter 元数据的变更提案（Dry-Run 无副作用）。

        Args:
            path: 目标笔记的相对路径。
            updates: 拟合并/修改的元数据键值映射字典。

        Returns:
            封装好的不可变 ChangeSet 提案。
        """
        target = self.path(path)
        content, original_hash = self._read(target)
        updated = patch_frontmatter(content, updates)
        return ChangeSet(FileChange("frontmatter", path, None, original_hash, content, updated))

    def _backlink_impact(self, path: str) -> tuple[str, ...]:
        """推导移动或重命名指定笔记时全库受影响的反向 WikiLink 来源列表。

        遍历知识库中除当前笔记外的所有 Markdown 文件，解析其 wikilinks 链接：
        - 匹配完整无后缀路径（如 'Folder/Note'）；
        - 匹配短文件名（如 'Note'，Obsidian 默认短链接模式）；
        - 匹配基于当前来源文件的同级相对路径。

        Returns:
            所有包含指向该笔记有效链接的外部笔记相对路径元组（按字典序排列）。
        """
        old = PurePosixPath(path)
        old_without_suffix = str(old.with_suffix(""))
        affected: set[str] = set()
        for candidate in scan_markdown_files(self.root):
            source = candidate.relative_to(self.root).as_posix()
            if source == path:
                continue
            note = parse_note(candidate, vault_root=self.root)
            for link in note.wikilinks:
                target = link.target_path
                if not target:
                    continue
                normalized = target[:-3] if target.lower().endswith(".md") else target
                if normalized in (old_without_suffix, old.stem):
                    affected.add(source)
                    break
                relative_target = str(PurePosixPath(source).parent / normalized)
                if relative_target == old_without_suffix:
                    affected.add(source)
                    break
        return tuple(sorted(affected))

    def move_note(self, path: str, destination: str) -> ChangeSet:
        """生成移动或重命名笔记的变更提案（Dry-Run 无副作用）。

        自动计算全库反向链接影响范围并挂载在提案中，供审查或事务级联重写。

        Args:
            path: 原始笔记相对路径。
            destination: 目标笔记相对路径。

        Returns:
            封装好的不可变 ChangeSet 提案。

        Raises:
            SafeWriteError: 当源路径与目标路径完全相同时抛出。
        """
        source = self.path(path)
        target = self.path(destination)
        if source == target:
            raise SafeWriteError("Source and destination are identical")
        content, original_hash = self._read(source)
        self._vacant(target)
        impacts = self._backlink_impact(path)
        return ChangeSet(FileChange("move", path, destination, original_hash, content, content, impacts))

    def trash_note(self, path: str) -> ChangeSet:
        """生成安全软删除（移入隔离废纸篓）的变更提案（Dry-Run 无副作用）。

        不直接物理删除，而是通过 UUID 分配隔离路径 `.obsai-trash/{uuid}/{path}`，杜绝同名文件覆盖。

        Args:
            path: 待删除笔记相对路径。

        Returns:
            封装好的不可变 ChangeSet 提案。
        """
        source = self.path(path)
        content, original_hash = self._read(source)
        destination = f".obsai-trash/{uuid4().hex}/{path}"
        self._vacant(self.path(destination, internal=True))
        return ChangeSet(FileChange("trash", path, destination, original_hash, content, None))

    def preview(self, change: ChangeSet, console: Console) -> None:
        """在 Rich 控制台中输出高保真彩色差异对比预览。

        Args:
            change: 待审查的变更集对象。
            console: Rich 终端控制台实例。
        """
        for line in change_preview_lines(change):
            console.print(line.text, style=line.style, markup=False, highlight=line.highlight)

    def _atomic_write(self, path: Path, data: bytes, *, expected_hash: str | None) -> None:
        """执行工业级原子、抗崩溃文件持久化落盘。

        原子落盘工作流：
        1. 在目标父目录内创建 `.obsai-*.tmp` 临时文件（确保在同物理分区，支持原子系统调用）；
        2. 将二进制数据写入临时文件并执行 flush；
        3. 若为更新操作，克隆原文件的 POSIX 权限位（chmod）；
        4. 对临时文件调用 os.fsync 强制物理刷盘；
        5. 临门一脚通过 _check_current 重新验证当前文件状态与 expected_hash（CAS 防脏写）；
        6. 落盘操作：
           - 全新创建（expected_hash is None）：调用 `os.link(temporary, path)` 原子无覆写占位；
             若并发碰撞会抛出 FileExistsError，杜绝无意覆盖；随后删除临时文件；
           - 覆写更新（expected_hash is not None）：调用 `os.replace(temporary, path)` 原子替换；
        7. 调用 _sync_directory 刷盘父目录项（dentry）；
        8. 在 finally 块中确保异常退出时临时文件不残留。

        Raises:
            CollisionError: 目标路径在原子落盘瞬间发生并发碰撞已存在；
            SafeWriteError: 底层落盘 I/O 失败；
            ConflictError: CAS 校验未通过（文件被外部并发修改或消失）。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".obsai-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                if expected_hash is not None:
                    try:
                        temporary.chmod(stat.S_IMODE(path.stat().st_mode))
                    except FileNotFoundError as exc:
                        raise ConflictError(f"Note disappeared: {path.relative_to(self.root)}") from exc
                os.fsync(handle.fileno())
            relative = path.relative_to(self.root)
            self.path(relative.as_posix(), internal=relative.parts[0] == ".obsai-trash")
            self._check_current(path, expected_hash)
            if expected_hash is None:
                # Atomic no-clobber create; unlike replace, this cannot overwrite a racing file.
                os.link(temporary, path)
                temporary.unlink()
            else:
                os.replace(temporary, path)
            _sync_directory(path.parent)
        except FileExistsError as exc:
            raise CollisionError(f"Destination already exists: {path.relative_to(self.root)}") from exc
        except OSError as exc:
            raise SafeWriteError(f"Cannot commit note {path.relative_to(self.root)}: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _check_current(self, path: Path, expected_hash: str | None) -> None:
        """乐观并发控制（CAS）核心校验：比对物理磁盘文件的当前哈希与预期哈希。

        Args:
            path: 物理文件路径。
            expected_hash: 期望的 SHA-256 哈希值；若为 None 表示预期目标文件应当不存在（创建操作）。

        Raises:
            CollisionError: 预期不存在但实际已存在；
            ConflictError: 文件已被外部并发修改（哈希不一致）、已消失或不再是常规文件；
            SafeWriteError: 无法重查文件状态。
        """
        if expected_hash is None:
            self._vacant(path)
            return
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ConflictError(f"Note is no longer a regular file: {path.relative_to(self.root)}")
            current = path.read_bytes()
        except FileNotFoundError as exc:
            raise ConflictError(f"Note disappeared: {path.relative_to(self.root)}") from exc
        except OSError as exc:
            raise SafeWriteError(f"Cannot recheck note {path.relative_to(self.root)}: {exc}") from exc
        if _hash(current) != expected_hash:
            raise ConflictError(f"Note changed since preview: {path.relative_to(self.root)}")

    def apply(self, change: ChangeSet, *, approved: bool) -> bool:
        """执行物理变更落盘，受显式人工确认守卫（Approval Guard）保护。

        执行流程：
        1. 确认检查：若 `approved=False`，立即安全退出，绝对不产生任何物理磁盘副作用；
        2. 操作路由与临门一脚 CAS 校验：
           - create: 校验参数有效性，执行 `_atomic_write` 原子创建；
           - update / frontmatter: 校验原哈希，CAS 校验无冲突后执行原子替换；
           - move / trash:
             - 校验源文件 CAS 哈希与目标路径空置；
             - 递归创建目标父目录；
             - 通过 `os.link(source, destination)` 建立目标硬链接；
             - 解除源文件链接 `source.unlink()`；若解除失败，立即补偿删除已建立的目标硬链接并抛出异常；
             - 刷盘源目录与目标目录的目录项（_sync_directory）。

        Args:
            change: 待应用的变更集对象。
            approved: 是否获得用户或上层显式批准。

        Returns:
            若成功应用返回 True；若未经批准被取消则返回 False。

        Raises:
            SafeWriteError: 提案参数不合法或包含未知操作；
            ConflictError / CollisionError: 并发冲突。
        """
        if not approved:
            return False
        file = change.file
        source = self.path(file.path)
        if file.operation == "create":
            if file.original_hash is not None or file.new_content is None:
                raise SafeWriteError("Invalid create proposal")
            self._check_current(source, None)
            self._atomic_write(source, _encode(file.new_content), expected_hash=None)
        elif file.operation in ("update", "frontmatter"):
            if file.original_hash is None or file.new_content is None:
                raise SafeWriteError("Invalid update proposal")
            self._check_current(source, file.original_hash)
            self._atomic_write(source, _encode(file.new_content), expected_hash=file.original_hash)
        elif file.operation in ("move", "trash"):
            if file.original_hash is None or file.destination is None:
                raise SafeWriteError("Invalid move proposal")
            destination = self.path(file.destination, internal=file.operation == "trash")
            self._check_current(source, file.original_hash)
            self._vacant(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            self.path(file.destination, internal=file.operation == "trash")
            self._check_current(source, file.original_hash)
            try:
                os.link(source, destination)
            except FileExistsError as exc:
                raise CollisionError(f"Destination already exists: {file.destination}") from exc
            except OSError as exc:
                raise SafeWriteError(f"Cannot move note to {file.destination}: {exc}") from exc
            try:
                source.unlink()
            except OSError:
                destination.unlink(missing_ok=True)
                raise
            _sync_directory(source.parent)
            _sync_directory(destination.parent)
        else:
            raise SafeWriteError(f"Unknown operation: {file.operation}")
        return True
