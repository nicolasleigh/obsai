"""Read optional TOML config without creating files or directories.

该模块负责以纯只读方式发现、解析 TOML 配置文件并与环境变量聚合生成全局 Settings 实例。

核心设计原则：
1. 纯只读与零副作用（Zero Side-effects）：
   本模块绝不主动创建任何目录（如 mkdir）或写入空配置文件，确保在只读文件系统或受限权限环境中安全运行。
2. 多级就近查找与 XDG 规范（Cascading Resolution & XDG Specification）：
   - 最高优先级：环境变量 ``OBSAI_CONFIG_PATH`` 显式指定的配置文件路径；
   - 本地项目就近查找：当前工作目录（CWD）下的 ``obsai.toml`` 或 ``.obsai/config.toml``；
   - 全局兜底规范：优先尊重 ``$XDG_CONFIG_HOME`` 环境变量，若未设定则回退至用户目录下的 ``~/.config/obsai/config.toml``。
3. 显式指定 vs 隐式默认的差异化容错策略：
   - 若调用方显式传入了 path 或配置了 ``OBSAI_CONFIG_PATH``，当文件不存在时视为明确的操作失误，必须立刻抛出 ConfigError。
   - 若使用隐式默认路径且文件不存在，则属于标准的“零配置（Zero-Config）”开箱即用场景，静默回退为空字典，依靠默认值与环境变量运行。
4. 单一错误抽象（Unified Error Abstraction）：
   无论是底层 IO 异常（OSError）、语法错误（TOMLDecodeError）、校验失败（ValidationError）还是环境配置错误（SettingsError），
   统一包装为领域异常 ConfigError，并使用 Python 3 的异常链（from exc）保留完整上下文。
"""

import os
import tomllib
from pathlib import Path

from pydantic import ValidationError
from pydantic_settings import SettingsError

from obsai.config.models import Settings
from obsai.errors import ConfigError

#: 本地项目目录中优先查找的配置文件候选文件名列表
LOCAL_CONFIG_NAMES: tuple[str, ...] = ("obsai.toml", ".obsai/config.toml")


def default_config_path() -> Path:
    """计算并返回系统的默认全局配置文件路径。

    遵循 XDG 基础目录规范（XDG Base Directory Specification）：
    1. 若系统设置了 XDG_CONFIG_HOME 环境变量，展开其中的用户路径后作为基准目录；
    2. 若未设定，则默认回退至用户家目录下的 ~/.config；
    3. 最终拼接子目录与文件名：{base}/obsai/config.toml。

    :return: 默认全局配置文件的 Path 对象
    """
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "obsai" / "config.toml"


def discover_config_path(start_dir: Path | None = None) -> tuple[Path, bool]:
    """按照多级就近查找策略解析配置文件路径。

    查找优先级：
    1. 环境变量 ``OBSAI_CONFIG_PATH``：显式指定路径，若配置此环境变量则文件必须存在，否则抛出 ConfigError。
    2. 当前工作目录（CWD）：探测 ``obsai.toml`` 或 ``.obsai/config.toml``，存在即采用。
    3. 全局 XDG 默认配置路径：回退至 :func:`default_config_path`（文件不存在时允许优雅降级为零配置）。

    Args:
        start_dir: 探测本地配置的基准目录，默认使用 :func:`Path.cwd`。

    Returns:
        tuple[Path, bool]:
            - 第 1 项：计算出的配置文件路径。
            - 第 2 项：该配置文件在磁盘上是否真实存在（True 表示存在，False 表示不存在）。

    Raises:
        ConfigError: 当环境变量 ``OBSAI_CONFIG_PATH`` 指定的文件不存在时抛出。
    """
    env_config = os.environ.get("OBSAI_CONFIG_PATH")
    if env_config:
        explicit_path = Path(env_config).expanduser()
        if not explicit_path.is_file():
            raise ConfigError(
                f"Configuration file specified by OBSAI_CONFIG_PATH does not exist: {explicit_path}"
            )
        return explicit_path, True

    base_dir = start_dir if start_dir is not None else Path.cwd()
    for candidate_name in LOCAL_CONFIG_NAMES:
        candidate = base_dir / candidate_name
        if candidate.is_file():
            return candidate, True

    xdg_path = default_config_path()
    if xdg_path.is_file():
        return xdg_path, True

    return xdg_path, False


def load_settings(path: Path | None = None) -> Settings:
    """以只读安全方式加载配置并返回 Settings 模型实例。

    加载流程：
    1. 确定配置路径（显式指定的 path 优先，否则调用 :func:`discover_config_path` 自动发现）；
    2. 以二进制流只读打开 TOML 文件并解析为原生 Python 字典；
    3. 若遇文件缺失，对显式传入路径报错，对隐式发现但不存在的全局路径优雅降级为空字典；
    4. 实例化 Pydantic Settings，自动合并 TOML 数据、默认值与 OBSAI_* 环境变量；
    5. 捕获所有底层和校验异常，统一包装为 ConfigError 抛出。

    :param path: 可选的自定义配置文件路径。若为 None 则自动按多级策略探测。
    :return: 经过严密校验的全局 Settings 实例。
    :raises ConfigError: 当显式指定的文件不存在、文件无法读取、TOML 语法损坏或配置项校验未通过时抛出。
    """
    if path is not None:
        config_path = path
        if not config_path.is_file():
            raise ConfigError(f"Configuration file not found: {config_path}")
        exists = True
    else:
        config_path, exists = discover_config_path()

    if not exists:
        data = {}
    else:
        try:
            # 必须以二进制只读模式 ("rb") 打开，tomllib.load 严格要求字节流并按 UTF-8 解码，防止跨平台编码问题
            with config_path.open("rb") as config_file:
                data = tomllib.load(config_file)
        except OSError as exc:
            # 捕获权限受限、文件损坏等 IO 异常
            raise ConfigError(f"Cannot load configuration {config_path}: {exc}") from exc
        except tomllib.TOMLDecodeError as exc:
            # 捕获 TOML 格式解析错误
            raise ConfigError(f"Cannot load configuration {config_path}: {exc}") from exc

    try:
        # 实例化 Settings：触发 Pydantic 字段验证、OBSAI_* 环境变量多源合并及 extra="forbid" 检查
        return Settings(**data)
    except (ValidationError, SettingsError) as exc:
        # 统一封装 Pydantic 校验错误，向调用方提供清晰的配置诊断信息
        raise ConfigError(f"Invalid configuration {config_path}: {exc}") from exc
