"""Cooperative SIGINT/SIGTERM handling with deferred critical filesystem units."""

import signal
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


class ShutdownRequested(BaseException):
    def __init__(self, signum: int):
        super().__init__(f"Shutdown requested by signal {signum}")
        self.signum = signum

    @property
    def exit_code(self) -> int:
        return 128 + self.signum


_active: ContextVar["ShutdownController | None"] = ContextVar("obsai_shutdown", default=None)


class ShutdownController:
    def __init__(self) -> None:
        self.requested = threading.Event()
        self.signum: int | None = None
        self._defer_depth = 0
        self._previous: dict[int, object] = {}
        self._token = None

    def __enter__(self) -> "ShutdownController":
        self._token = _active.set(self)
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                self._previous[signum] = signal.getsignal(signum)
                signal.signal(signum, self._handle)
        return self

    def __exit__(self, *_: object) -> None:
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)
        self._previous.clear()
        if self._token is not None:
            _active.reset(self._token)

    def _handle(self, signum: int, _frame: object) -> None:
        self.request(signum)

    def request(self, signum: int = signal.SIGINT) -> None:
        self.signum = self.signum or signum
        self.requested.set()
        if not self._defer_depth:
            self.check()

    def check(self) -> None:
        if self.requested.is_set():
            raise ShutdownRequested(self.signum or signal.SIGINT)

    @contextmanager
    def defer(self) -> Iterator[None]:
        self._defer_depth += 1
        try:
            yield
        finally:
            self._defer_depth -= 1


def check_shutdown() -> None:
    controller = _active.get()
    if controller is not None:
        controller.check()


@contextmanager
def defer_shutdown() -> Iterator[None]:
    controller = _active.get()
    if controller is None:
        yield
    else:
        with controller.defer():
            yield
