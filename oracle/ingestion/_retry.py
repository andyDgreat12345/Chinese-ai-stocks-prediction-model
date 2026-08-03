"""Retry with exponential backoff — market-data / RSS endpoints flake (spec §6.2)."""
from __future__ import annotations

import time
from functools import wraps
from typing import Callable, TypeVar

T = TypeVar("T")


def with_retries(
    attempts: int = 4,
    base_delay: float = 2.0,
    exc: tuple[type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], None] = time.sleep,
):
    """Retry a callable up to `attempts` times, doubling the delay each time
    (2s, 4s, 8s, ...). Re-raises the last exception if all attempts fail.

    `sleep` is injectable so tests don't actually wait.
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last: BaseException | None = None
            for i in range(attempts):
                try:
                    return func(*args, **kwargs)
                except exc as e:  # noqa: PERF203
                    last = e
                    if i < attempts - 1:
                        sleep(base_delay * (2 ** i))
            assert last is not None
            raise last
        return wrapper
    return decorator
