from __future__ import annotations

import time
from typing import Callable, TypeVar

from .logs import get_logger

T = TypeVar("T")
log = get_logger("retry")


def retry_call(fn: Callable[[], T], *, attempts: int = 3, base_delay: float = 1.0,
               retry_on: tuple[type[BaseException], ...] = (Exception,),
               sleep: Callable[[float], None] = time.sleep, what: str = "call") -> T:
    """Call fn, retrying with exponential backoff (base, 2x base, 4x base...)."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            if attempt == attempts:
                raise
            delay = base_delay * 2 ** (attempt - 1)
            log.warning("%s failed (%s); retry %d/%d in %.0fs", what, exc, attempt, attempts - 1, delay)
            sleep(delay)
    raise AssertionError("unreachable")
