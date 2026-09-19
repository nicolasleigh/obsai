"""低开销结构化指标采集与性能遥测模块；严格杜绝笔记内容、查询语句、物理路径或密钥泄漏。

该模块为 ObsAI 提供轻量级、高精度的性能监控与埋点统计基础设施。

核心设计原则：
1. **隐私优先与零泄漏（Privacy by Design & Zero Data Leakage）**：
   - 绝不记录任何敏感数据：严禁在指标中输出笔记内容（prose）、搜索查询原词（queries）、
     本地文件绝对路径（paths）或 API 密钥凭据（secrets）。
   - 仅记录操作名称（name）、执行耗时（duration_ms）、操作结果状态（status="ok"|"error"）
     以及脱敏后的聚合数值（如块数量 chunk_count、Token 估算量、证据条数等）。
2. **近零运行时开销（Zero Overhead When Inactive）**：
   - 使用专用的命名空间记录器 ``obsai.metrics``。
   - 在日志级别未达到 ``INFO`` 时，利用 ``logger.isEnabledFor(logging.INFO)`` 短路判断，
     避免字典构造、JSON 序列化与字符串格式化带来的 CPU 开销。
3. **结构化与机器可读（Structured JSON Logging）**：
   - 所有指标均以单行 JSON 格式输出，并对键进行排序（``sort_keys=True``），
     便于日志采集分析工具（如 Loki, Datadog, ELK 或 jq）进行自动提取与时序图表聚合。
4. **多形态耗时监控（Flexible Instrumentation）**：
   - 提供上下文管理器 :func:`measure`、同步函数装饰器 :func:`measured` 与异步协程装饰器 :func:`measured_async`，
     全方位覆盖关键调用链路（嵌入生成、向量检索、混合图遍历、大模型交互）。
"""

import functools
import json
import logging
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

#: 遥测指标专用的命名空间记录器，可独立配置日志级别或输出重定向
logger = logging.getLogger("obsai.metrics")


def metric(name: str, **values: int | float | str | bool) -> None:
    """输出单条结构化指标记录。

    若 ``obsai.metrics`` 记录器未启用 ``INFO`` 级别，则直接短路跳过，实现近零开销。
    指标内容将自动序列化为 JSON 字符串并以标准日志输出。

    Args:
        name: 指标名称（建议采用点分命名法，如 ``vector.search``, ``pipeline.embed``）。
        **values: 附加的指标度量值或无隐私标签键值对（支持 int, float, str, bool）。
    """
    if logger.isEnabledFor(logging.INFO):
        payload: dict[str, Any] = {"name": name, **values}
        logger.info("metric %s", json.dumps(payload, sort_keys=True))


@contextmanager
def measure(name: str, **values: int | float | str | bool) -> Iterator[None]:
    """高精度耗时与状态度量上下文管理器。

    通过 :func:`time.perf_counter` 统计代码块的运行耗时。在代码块退出时（无论正常结束或发生异常），
    均会在 ``finally`` 中自动上报包含 ``duration_ms``（毫秒，保留 3 位小数）和 ``status``
    （``"ok"`` 或 ``"error"``）的指标。

    若代码块抛出异常（包括派生自 :class:`BaseException` 的中断），将记录 ``status="error"``
    并将异常透明向上重抛，绝不静默拦截。

    Args:
        name: 测量的操作名称。
        **values: 随同耗时指标一并上报的静态度量值或标签。

    Yields:
        None: 进入被保护代码块。
    """
    start = time.perf_counter()
    status = "ok"
    try:
        yield
    except BaseException:
        status = "error"
        raise
    finally:
        metric(
            name,
            duration_ms=round((time.perf_counter() - start) * 1000, 3),
            status=status,
            **values,
        )


def measured(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """同步函数耗时测量装饰器。

    使用 :func:`measure` 包装目标同步函数，自动收集该函数调用的耗时与成功/失败状态。

    Args:
        name: 测量的操作指标名称。

    Returns:
        Callable: 包装后的装饰器函数。
    """
    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(function)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            with measure(name):
                return function(*args, **kwargs)

        return wrapped

    return decorate


def measured_async(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """异步协程函数耗时测量装饰器。

    使用 :func:`measure` 包装目标异步协程，自动收集 await 执行期间的耗时与状态。

    Args:
        name: 测量的操作指标名称。

    Returns:
        Callable: 包装后的异步装饰器函数。
    """
    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(function)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            with measure(name):
                return await function(*args, **kwargs)

        return wrapped

    return decorate
