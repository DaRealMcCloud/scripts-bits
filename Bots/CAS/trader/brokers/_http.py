"""Shared HTTP + retry utilities for broker adapters.

Provides:
- :func:`with_retry` — exponential backoff decorator handling transient errors
  and HTTP 429 (rate limit) responses.
- :class:`RateLimiter` — a simple token-bucket to stay under a req/s ceiling
  (IBKR enforces a global 10 req/s limit).
"""

from __future__ import annotations

import functools
import logging
import random
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RateLimitError(RuntimeError):
    """Raised internally to signal an HTTP 429 that should be retried."""


class TransientError(RuntimeError):
    """A transient network/server error that is safe to retry."""


def with_retry(
    max_attempts: int = 5,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
    retry_on: tuple[type[Exception], ...] = (TransientError, RateLimitError),
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator: retry a callable with exponential backoff + jitter."""

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            attempt = 0
            while True:
                attempt += 1
                try:
                    return func(*args, **kwargs)
                except retry_on as exc:
                    if attempt >= max_attempts:
                        logger.error(
                            "%s failed after %d attempts: %s",
                            func.__name__,
                            attempt,
                            exc,
                        )
                        raise
                    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    delay += random.uniform(0, delay * 0.25)  # jitter
                    logger.warning(
                        "%s attempt %d/%d failed (%s); retrying in %.2fs",
                        func.__name__,
                        attempt,
                        max_attempts,
                        exc,
                        delay,
                    )
                    time.sleep(delay)

        return wrapper

    return decorator


class RateLimiter:
    """Thread-safe token bucket limiting calls to ``rate`` per second."""

    def __init__(self, rate: float = 9.0) -> None:
        self._min_interval = 1.0 / rate if rate > 0 else 0.0
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
