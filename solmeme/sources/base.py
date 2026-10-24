"""Source plumbing: context passed to every source, per-source exponential
backoff (port of PaperTrader monitor_backoff.py), and a token bucket."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

from solmeme.config import Settings

log = logging.getLogger("solmeme.sources")


class Backoff:
    def __init__(self, base_s: float = 5.0, max_s: float = 600.0):
        self.base_s, self.max_s = base_s, max_s
        self.failures = 0
        self.until = 0.0
        self.last_error = ""

    def ok(self) -> None:
        self.failures = 0
        self.until = 0.0
        self.last_error = ""

    def fail(self, err: str, retry_after: float | None = None) -> float:
        self.failures += 1
        self.last_error = err[:300]
        wait = retry_after if retry_after else min(self.max_s, self.base_s * 2 ** (self.failures - 1))
        wait *= random.uniform(0.8, 1.2)
        self.until = time.time() + wait
        return wait

    def ready(self) -> bool:
        return time.time() >= self.until


class TokenBucket:
    def __init__(self, rate_per_s: float, capacity: float | None = None):
        self.rate = rate_per_s
        self.capacity = capacity or max(1.0, rate_per_s)
        self.tokens = self.capacity
        self.t = time.monotonic()
        self._lock = asyncio.Lock()

    async def take(self, n: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                await asyncio.sleep((n - self.tokens) / self.rate)


@dataclass
class SourceStats:
    events: int = 0
    errors: int = 0
    last_event_ts: float = 0.0
    status: str = "starting"
    note: str = ""


@dataclass
class Context:
    settings: Settings
    http: httpx.AsyncClient
    emit: Callable
    watch: object = None                                   # Watchlist
    stats: dict[str, SourceStats] = field(default_factory=dict)
    activity: object = None
    holders_fn: object = None                              # mint -> {wallet: balance} from the trade stream (the session sets it)
    whales: set = field(default_factory=set)               # every whale in the book; the session refreshes it

    def stat(self, name: str) -> SourceStats:
        return self.stats.setdefault(name, SourceStats())

    def push(self, name: str, ev) -> None:
        s = self.stat(name)
        s.events += 1
        s.last_event_ts = time.time()
        s.status = "ok"
        self.emit(ev)


def retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers.get("retry-after", ""))
    except ValueError:
        return None


async def poll_loop(ctx: Context, name: str, period_s: float, fn, backoff: Backoff | None = None) -> None:
    """Run `fn()` every period with backoff on failure; never raises."""
    bo = backoff or Backoff()
    while True:
        if bo.ready():
            try:
                await fn()
                bo.ok()
            except httpx.HTTPStatusError as exc:
                wait = bo.fail(f"HTTP {exc.response.status_code}", retry_after(exc.response))
                st = ctx.stat(name)
                st.errors += 1
                st.status, st.note = "backoff", f"HTTP {exc.response.status_code}; retry in {wait:.0f}s"
                log.warning("%s: %s (retry in %.0fs)", name, exc, wait)
            except Exception as exc:  # noqa: BLE001 — a source must never kill the run
                wait = bo.fail(repr(exc))
                st = ctx.stat(name)
                st.errors += 1
                st.status, st.note = "backoff", f"{type(exc).__name__}: {str(exc)[:120]}"
                log.warning("%s: %r (retry in %.0fs)", name, exc, wait)
        await asyncio.sleep(period_s)
