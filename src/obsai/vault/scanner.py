"""确定性 Vault 仓库文件扫描器；排除隐藏文件、符号链接及忽略规则匹配的笔记。

该模块负责以安全、确定且高效的方式遍历 Obsidian Vault 目录树，筛选出符合条件的 Markdown 文件，
并提供批量解析为 AST（:class:`~obsai.vault.models.ParsedNote`）的入口。

核心安全与设计原则：
1. **安全边界与符号链接防护（Symlink Hardening）**：
   - 绝不跟随目录符号链接（``followlinks=False``），防止通过软链接逃逸 Vault 根目录或陷入无限循环递归。
   - 明确过滤文件级符号链接（``is_symlink()``），确保所有被索引的文档均为 Vault 内的物理实体。
   - 拒绝符号链接形式的 ``.obsaiignore`` 配置文件，防止规则劫持。
2. **隐藏文件与系统目录排除（Dotfile/Hidden Exclusion）**：
   - 默认忽略所有以点号 ``.`` 开头的文件和目录（如 ``.git/``、``.obsidian/``、``.trash/``、``.DS_Store`` 等）。
3. **基于 GitIgnore 规范的过滤机制（PathSpec Integration）**：
   - 支持根目录下的 ``.obsaiignore`` 文件，采用与 ``.gitignore`` 完全兼容的通配符语法。
   - 目录过滤时追加 ``/``（如 ``nested/``），确保目录级排除规则精准生效。
4. **剪枝优化（Directory Pruning）**：
   - 在 ``os.walk`` 遍历过程中，原地修改 ``dirs[:]`` 列表进行剪枝，
     被排除的目录（隐藏目录、符号链接、被忽略目录）及其内部所有子孙文件将永远不会被读取或 stat，极大降低磁盘 I/O。
5. **确定性与可重现排序（Deterministic Ordering）**：
   - 目录遍历与最终返回的文件列表均按照相对于 Vault 根目录的 POSIX 路径进行字典序升序排序，
     保证跨平台、跨操作系统环境下扫描结果完全一致。
"""

import os
from pathlib import Path

from pathspec import GitIgnoreSpec

from obsai.errors import VaultError
from obsai.vault.models import ParsedNote
from obsai.vault.parser import parse_note


def _vault_root(root: Path) -> Path:
    """校验并解析 Vault 仓库的物理根目录路径。

    解析绝对真实路径并确保目标为一个有效存在的目录。

    Args:
        root: Vault 根目录的路径（相对路径或绝对路径）。

    Returns:
        Path: 解析并展开符号链接后的绝对物理目录路径。

    Raises:
        VaultError: 当路径无法访问、不存在、或不是一个目录时抛出。
    """
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise VaultError(f"Cannot access vault {root}: {exc}") from exc
    if not resolved.is_dir():
        raise VaultError(f"Vault is not a directory: {root}")
    return resolved


def _ignore_spec(root: Path) -> GitIgnoreSpec:
    """加载并编译 Vault 根目录下的 ``.obsaiignore`` 忽略规则规范。

    规则语法完全遵循标准 ``.gitignore`` 规范。若文件不存在则返回空的匹配规范。

    Args:
        root: 已解析的 Vault 根目录路径。

    Returns:
        GitIgnoreSpec: 编译后的路径匹配规范对象。

    Raises:
        VaultError: 当 ``.obsaiignore`` 为符号链接、读取失败（非 UTF-8 或 I/O 错误）、或规则语法非法时抛出。
    """
    ignore_file = root / ".obsaiignore"
    # 防御性校验：拒绝符号链接形式的忽略配置文件，防止外部恶意规则注入
    if ignore_file.is_symlink():
        raise VaultError(f"Ignore file must not be a symlink: {ignore_file}")
    try:
        lines = ignore_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    except (OSError, UnicodeError) as exc:
        raise VaultError(f"Cannot read {ignore_file}: {exc}") from exc
    try:
        return GitIgnoreSpec.from_lines(lines)
    except ValueError as exc:
        raise VaultError(f"Invalid {ignore_file}: {exc}") from exc


def scan_markdown_files(root: Path) -> list[Path]:
    """扫描 Vault 仓库并返回确定性排序的可读 Markdown 文件路径列表。

    遍历过程中会对排除的目录进行原地剪枝，绝不会深入遍历被忽略、隐藏或符号链接的目录。

    扫描规则与过滤策略：
    - **目录剪枝（Pruning）**：
      * 排除以点开头的隐藏目录（如 ``.obsidian``, ``.git``）。
      * 排除目录级符号链接。
      * 排除符合 ``.obsaiignore`` 目录匹配规则的路径。
    - **文件过滤**：
      * 排除以点开头的隐藏文件。
      * 必须以 ``.md`` 结尾（不区分大小写）。
      * 排除符号链接文件。
      * 排除符合 ``.obsaiignore`` 文件匹配规则的路径。
    - **结果排序**：
      * 最终返回的文件列表按相对于 Vault 根目录的 POSIX 路径进行字典序排序。

    Args:
        root: Vault 仓库根目录路径。

    Returns:
        list[Path]: 排序后的合法 Markdown 文件绝对路径列表。

    Raises:
        VaultError: 当 Vault 根目录不可访问、扫描过程中发生系统 I/O 错误、或 ignore 文件非法时抛出。
    """
    root = _vault_root(root)
    ignore = _ignore_spec(root)
    paths: list[Path] = []

    def on_error(exc: OSError) -> None:
        """遍历异常回调函数，转换为统一的 VaultError。"""
        raise VaultError(f"Cannot scan vault {root}: {exc}") from exc

    # 使用 os.walk 进行深度优先遍历，followlinks=False 确保绝不跟踪目录符号链接
    for parent, dirs, files in os.walk(root, followlinks=False, onerror=on_error):
        directory = Path(parent)
        # 原地修改 dirs[:] 列表以阻止 os.walk 进入被排除的目录（性能剪枝）
        dirs[:] = sorted(
            name
            for name in dirs
            if not name.startswith(".")
            and not (directory / name).is_symlink()
            and not ignore.match_file((directory / name).relative_to(root).as_posix() + "/")
        )
        for name in sorted(files):
            # 仅保留非隐藏的 Markdown 笔记文件
            if name.startswith(".") or not name.lower().endswith(".md"):
                continue
            path = directory / name
            # 排除符号链接以及被 .obsaiignore 命中的文件
            if path.is_symlink() or ignore.match_file(path.relative_to(root).as_posix()):
                continue
            paths.append(path)

    # 按相对于 Vault 根目录的 POSIX 相对路径进行全局确定性排序
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def parse_vault(root: Path) -> list[ParsedNote]:
    """扫描并批量解析 Vault 仓库中所有被接纳的合法 Markdown 笔记。

    先通过 :func:`scan_markdown_files` 进行安全的确定性文件发现，
    再逐一调用 :func:`~obsai.vault.parser.parse_note` 将各笔记解析为 AST 对象模型。

    Args:
        root: Vault 仓库根目录路径。

    Returns:
        list[ParsedNote]: 所有已解析笔记的 AST 对象列表，与扫描路径顺序严格一致。

    Raises:
        VaultError: 当扫描文件或读取笔记发生 I/O、权限或安全错误时抛出。
        ParseError: 当某篇笔记的内部语法或模型校验严重失败时抛出。
    """
    root = _vault_root(root)
    return [parse_note(path, vault_root=root) for path in scan_markdown_files(root)]
