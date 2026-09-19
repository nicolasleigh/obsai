"""CLI 终端日志系统配置模块；模块导入时无副作用（Zero Side Effects on Import）。

该模块为 ObsAI 命令行界面（CLI）提供轻量且美观的终端日志输出配置支持。

核心设计原则：
1. **模块导入无副作用（Side-Effect Free Import）**：
   - 导入本模块时不会自动调用 ``logging.basicConfig`` 或附加任何处理器（Handler）。
   - 必须由应用入口显式调用 :func:`configure_logging` 才会初始化根日志记录器，
     避免作为库被第三方引用或在单元测试运行时污染全局 logging 状态。
2. **终端美观渲染（Rich Terminal Formatting）**：
   - 集成 ``rich.logging.RichHandler``，提供彩色等级标签与格式化高亮。
   - 显式设置 ``show_path=False``，隐藏源码文件路径和行号信息，保持 CLI 命令行交互界面的干净整洁。
3. **环境变级动态覆盖（Environment-Driven Level Override）**：
   - 默认日志级别设为 ``logging.WARNING``，遵循 CLI 最佳实践——日常运行保持安静，仅在出现警告或错误时输出。
   - 支持通过环境变量 ``OBSAI_LOG_LEVEL``（可选值：``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``）
     在故障排查、调试或 CI/CD 环境下动态覆盖默认级别，无需修改配置文件或命令行参数。
"""

import logging
import os

from rich.logging import RichHandler


def configure_logging(level: int = logging.WARNING) -> None:
    """初始化并配置全局根日志记录器（Root Logger）。

    配置规则：
    1. 默认采用传入的 ``level`` 参数（缺省为 :data:`logging.WARNING`）。
    2. 优先检查环境变量 ``OBSAI_LOG_LEVEL``；若设定了合法的日志级别名称（大小写不敏感），
       则覆盖默认参数。
    3. 通过 :func:`logging.basicConfig` 绑定 :class:`rich.logging.RichHandler`，
       消息格式采用纯文本 ``%(message)s``，交由 Rich 负责终端染色与排版。

    Args:
        level: 默认的日志记录级别，默认为 :data:`logging.WARNING`。
    """
    configured = os.environ.get("OBSAI_LOG_LEVEL", "").upper()
    if configured in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        level = getattr(logging, configured)
    logging.basicConfig(level=level, handlers=[RichHandler(show_path=False)], format="%(message)s")
