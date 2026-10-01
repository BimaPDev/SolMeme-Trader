"""Clock abstraction: the engine only asks the clock for 'now', so live and
replay share every code path."""

from __future__ import annotations

import time


class LiveClock:
    def now(self) -> float:
        return time.time()


class ReplayClock:
    def __init__(self, start: float = 0.0):
        self._now = start

    def now(self) -> float:
        return self._now

    def set(self, ts: float) -> None:
        if ts > self._now:
            self._now = ts
