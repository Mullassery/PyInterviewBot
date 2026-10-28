"""Bounded in-process sliding-window rate limiting.

This is a single-process safeguard, not a distributed quota service. Deployments
with multiple workers should enforce a shared limit at the reverse proxy too.
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections import deque


class SlidingWindowLimiter:
    def __init__(
        self,
        limit: int,
        window_seconds: float = 60.0,
        max_keys: int = 10_000,
    ) -> None:
        """Initialize the limiter with its window and key bound."""
        self.limit = max(0, limit)
        self.window_seconds = max(1.0, window_seconds)
        self.max_keys = max(1, max_keys)
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> tuple[bool, int]:
        if self.limit == 0:
            return True, 0
        current = time.monotonic() if now is None else now
        cutoff = current - self.window_seconds
        with self._lock:
            if key not in self._hits and len(self._hits) >= self.max_keys:
                self._hits.clear()
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                retry_after = max(1, math.ceil(hits[0] + self.window_seconds - current))
                return False, retry_after
            hits.append(current)
            return True, 0


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def build_limiter() -> SlidingWindowLimiter:
    return SlidingWindowLimiter(
        limit=_env_int("PYINTERVIEWBOT_RATE_LIMIT", 120),
        window_seconds=float(_env_int("PYINTERVIEWBOT_RATE_WINDOW_SECONDS", 60)),
    )


__all__ = ["SlidingWindowLimiter", "build_limiter"]
