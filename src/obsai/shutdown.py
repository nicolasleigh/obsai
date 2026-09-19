"""协作式 SIGINT/SIGTERM 信号处理与临界区延迟关闭控制机制。

该模块提供了进程级与 Job 级别的优雅停机与取消控制，核心目标是在响应用户中断（如 Ctrl+C 或服务终止信号）
的同时，绝对保护底层文件系统的原子写入与事务一致性。

核心设计原则：
1. **协作式取消机制（Cooperative Cancellation）**：
   - 不依赖强制终止进程（kill -9 / SIGKILL），而是由工作流、批处理流水线与长时间任务在循环或关键步骤间歇
     主动调用 :func:`check_shutdown` 探测停机状态。
   - 一旦触发停机，抛出派生自 :class:`BaseException` 的受控异常，促使 Python 运行时的上下文管理器与
     `finally` 块正常执行资源清理、锁释放与事务回滚。
2. **临界区延期保护（Deferred Critical Sections）**：
   - 在执行物理文件写入、覆盖、原子重命名或事务日志提交等不可被半途中断的临界区时，
     使用 :func:`defer_shutdown` 上下文管理器将其保护起来。
   - 临界区执行期间，即使收到 `SIGINT` 或 `SIGTERM` 信号，也不会立即打断操作，而是将中断请求暂存；
     待临界区完全退出（延期深度归零）后，再统一抛出停机异常，杜绝“写入一半残损文件”的致命问题。
3. **分层取消抽象（CancellationCheck Protocol）**：
   - 定义通用的 :class:`CancellationCheck` 协议，使全局进程级 :class:`ShutdownController` 与单 Job
     作用域的局部取消令牌（Job CancellationToken）具备相同的行为契约（``check()`` 与 ``defer()``）。
   - 业务逻辑无需区分当前是在 CLI 中被用户按下 Ctrl+C，还是在 Web API 后台被单个任务终止，
     统一调用 :func:`check_shutdown` 与 :func:`defer_shutdown` 即可。
4. **异常层级隔离（BaseException vs Exception）**：
   - 停机异常（:class:`ShutdownRequested`、Job 取消、系统退出等）严格继承自 :class:`BaseException`
     而非 :class:`Exception`。这防止了业务代码中宽泛的 `except Exception:` 误吞停机信号导致进程僵死。
   - 提供 :func:`is_process_stop` 函数供基础事务层在捕获底层 `BaseException` 执行完回滚后安全重抛停机异常。
"""

import signal
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import ContextManager, Iterator, Protocol


class CancellationCheck(Protocol):
    """协作式取消检查契约协议。

    任何能够在协作式取消边界提供探针与延期保护的对象均需实现该协议。
    - :class:`ShutdownController` 提供了进程级的全局信号实现；
    - 针对异步任务或后台索引作业，则可提供 Job 粒度的局部取消令牌实现。
    两者均通过 :func:`check_shutdown` 统一暴露，使得流水线、工作流与索引器无需修改即可支持单任务取消。

    注意：
    ``defer`` 是该契约的核心组成部分，而非可选扩展；:func:`defer_shutdown` 依赖它来保护
    文件系统原子操作，若缺少它将破坏文件一致性保护。
    """

    def check(self) -> None:
        """检查是否已收到取消或停机请求；若已触发则抛出相应的中断异常。"""
        ...

    def defer(self) -> ContextManager[None]:
        """进入临界保护区，在此区域内暂缓响应取消信号，退出时恢复。"""
        ...


class ShutdownRequested(BaseException):
    """收到操作系统信号（如 SIGINT / SIGTERM）要求停机的控制流异常。

    继承自 :class:`BaseException` 以防止被宽泛的业务 `except Exception:` 块吞没。
    """

    def __init__(self, signum: int):
        """初始化停机异常。

        Args:
            signum: 触发停机的操作系统信号编号（如 signal.SIGINT, signal.SIGTERM）。
        """
        super().__init__(f"Shutdown requested by signal {signum}")
        self.signum = signum

    @property
    def exit_code(self) -> int:
        """根据标准 UNIX 惯例返回退出码（128 + 信号编号）。

        例如 SIGINT (2) -> 130，SIGTERM (15) -> 143。
        """
        return 128 + self.signum


#: 保存当前线程/协程上下文中活动取消控制器的上下文变量
_active: ContextVar[CancellationCheck | None] = ContextVar("obsai_shutdown", default=None)


class ShutdownController:
    """进程级协作式信号拦截与停机协调器。

    作为上下文管理器使用，在进入上下文时捕获 SIGINT 和 SIGTERM 信号，
    并在退出时恢复先前的系统信号处理器。
    """

    def __init__(self) -> None:
        """初始化控制器内部状态。"""
        self.requested = threading.Event()
        self.signum: int | None = None
        self._defer_depth = 0
        self._previous: dict[int, object] = {}
        self._token = None

    def __enter__(self) -> "ShutdownController":
        """激活控制器并安装信号处理器。

        仅在主线程中注册信号（操作系统限制信号只能在主线程注册）。
        """
        self._token = _active.set(self)
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                self._previous[signum] = signal.getsignal(signum)
                signal.signal(signum, self._handle)
        return self

    def __exit__(self, *_: object) -> None:
        """恢复先前的信号处理器并解绑上下文变量。"""
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)
        self._previous.clear()
        if self._token is not None:
            _active.reset(self._token)

    def _handle(self, signum: int, _frame: object) -> None:
        """底层操作系统信号处理回调函数。"""
        self.request(signum)

    def request(self, signum: int = signal.SIGINT) -> None:
        """记录停机请求。

        若当前未处于延期临界区（_defer_depth == 0），则立即执行 check 抛出异常；
        若处于延期保护中，则仅记录状态，延迟到临界区结束后再触发。

        Args:
            signum: 信号编号，缺省为 signal.SIGINT。
        """
        self.signum = self.signum or signum
        self.requested.set()
        if not self._defer_depth:
            self.check()

    def check(self) -> None:
        """检查停机标志；若已请求停机则抛出 :class:`ShutdownRequested` 异常。

        Raises:
            ShutdownRequested: 当已收到停机请求时抛出。
        """
        if self.requested.is_set():
            raise ShutdownRequested(self.signum or signal.SIGINT)

    @contextmanager
    def defer(self) -> Iterator[None]:
        """进入信号延期临界区。支持重入嵌套计数。"""
        self._defer_depth += 1
        try:
            yield
        finally:
            self._defer_depth -= 1


def check_shutdown() -> None:
    """在当前执行上下文中进行协作式停机/取消检查。

    各长耗时操作（如逐文件索引、批量嵌入计算、多轮问答迭代）应定期调用此函数，
    以便及时响应用户的取消操作。
    """
    controller = _active.get()
    if controller is not None:
        controller.check()


def is_process_stop(exc: BaseException) -> bool:
    """判定给定异常是否代表受控的进程停机或任务取消，而非真正的运行期故障。

    :class:`ShutdownRequested`、``JobCancelled``、:class:`KeyboardInterrupt` 与
    :class:`SystemExit` 均继承自 :class:`BaseException` 且不属于 :class:`Exception`。
    领域层代码在捕获 :class:`BaseException` 触发回滚操作后，必须通过此函数识别并将停机异常原样重新抛出，
    以便调用栈外层能够明确分辨出“操作执行失败”与“用户主动中断并已回滚”的区别。

    Args:
        exc: 捕获到的异常实例。

    Returns:
        bool: 若为进程终止或取消类异常则返回 True，否则返回 False。
    """
    return isinstance(exc, BaseException) and not isinstance(exc, Exception)


def current_controller() -> CancellationCheck | None:
    """获取当前上下文已安装的取消控制器实例（若未安装则返回 None）。"""
    return _active.get()


@contextmanager
def install_controller(controller: CancellationCheck) -> Iterator[None]:
    """在当前代码块作用域内临时安装指定的取消控制器。

    常用于 Job 执行器将取消边界限制在特定作业范围内。离开该代码块时（无论是正常结束还是异常退出），
    均会自动恢复原先的控制器。

    Args:
        controller: 待安装的取消控制器对象。
    """
    token = _active.set(controller)
    try:
        yield
    finally:
        _active.reset(token)


@contextmanager
def defer_shutdown() -> Iterator[None]:
    """保护临界区代码免受异步停机信号打断的上下文管理器。

    在执行文件系统原子写入、重命名、日志持久化等关键操作时使用。
    在此块执行期间，任何到来的中断信号都将被压制与暂存，直到块完全执行完毕后才触发停机。
    若当前未安装任何控制器，则退化为空操作。
    """
    controller = _active.get()
    if controller is None:
        yield
    else:
        with controller.defer():
            yield
