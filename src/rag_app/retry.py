"""Retry transient Databricks platform errors with exponential backoff.

Managed services occasionally return short-lived errors ("InternalError: Something went
wrong, please try again later", throttling, timeouts). Retrying a few times turns these
blips into a slightly slower answer instead of an error for the user. Permanent errors
(permission denied, not found, bad request) are raised immediately.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")

# databricks.sdk.errors class names that indicate a temporary condition
TRANSIENT_ERRORS = {
    "InternalError",
    "TemporarilyUnavailable",
    "TooManyRequests",
    "DeadlineExceeded",
    "ResourceExhausted",
    "Aborted",
    "Unknown",
}


def is_transient(exc: BaseException) -> bool:
    return any(cls.__name__ in TRANSIENT_ERRORS for cls in type(exc).__mro__)


def with_retry(fn: Callable[[], T], what: str, attempts: int = 3, base_delay: float = 0.5) -> T:
    """Call fn(); on a transient error wait 0.5s, 1s, ... (+ jitter) and try again."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == attempts or not is_transient(exc):
                raise
            delay = base_delay * 2 ** (attempt - 1) + random.uniform(0, base_delay / 2)  # noqa: S311
            log.warning(
                "%s failed (%s: %s); retry %d/%d in %.1fs", what, type(exc).__name__, exc, attempt, attempts - 1, delay
            )
            time.sleep(delay)
    raise AssertionError("unreachable")
