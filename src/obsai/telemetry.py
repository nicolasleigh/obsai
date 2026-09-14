"""Low-overhead structured metrics without note text, queries, paths, or secrets."""

import functools
import json
import logging
import time
from contextlib import contextmanager
from typing import Callable, Iterator


logger = logging.getLogger("obsai.metrics")


def metric(name: str, **values: int | float | str | bool) -> None:
    if logger.isEnabledFor(logging.INFO):
        logger.info("metric %s", json.dumps({"name": name, **values}, sort_keys=True))


@contextmanager
def measure(name: str, **values: int | float | str | bool) -> Iterator[None]:
    start = time.perf_counter()
    status = "ok"
    try:
        yield
    except BaseException:
        status = "error"
        raise
    finally:
        metric(name, duration_ms=round((time.perf_counter() - start) * 1000, 3),
               status=status, **values)


def measured(name: str) -> Callable:
    def decorate(function: Callable) -> Callable:
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            with measure(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def measured_async(name: str) -> Callable:
    def decorate(function: Callable) -> Callable:
        @functools.wraps(function)
        async def wrapped(*args, **kwargs):
            with measure(name):
                return await function(*args, **kwargs)
        return wrapped
    return decorate
