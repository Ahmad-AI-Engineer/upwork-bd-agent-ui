from __future__ import annotations

import asyncio
import time


class RateLimiter:
    def __init__(self, per_minute: float):
        self._min_interval = 60.0 / float(per_minute)
        self._lock = asyncio.Lock()
        self._last = 0.0

    @property
    def min_interval(self) -> float:
        return self._min_interval

    async def acquire(self) -> None:
        async with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    def pause(self, seconds: float) -> None:
        self._last = max(self._last, time.monotonic() + seconds - self._min_interval)
