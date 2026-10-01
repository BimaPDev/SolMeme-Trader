"""Small sources: SOL/USD price, optional Rugcheck, and placeholders for
platforms without an official data API (Axiom, j7tracker, fomo.family)."""

from __future__ import annotations

import time

from solmeme.config import secret
from solmeme.core.events import Enrichment, SolPrice
from solmeme.sources.base import Backoff, Context, TokenBucket, poll_loop

WSOL = "So11111111111111111111111111111111111111112"


def _note_check(ctx, mint: str, detail: str, symbol: str = "") -> None:
    act = getattr(ctx, "activity", None)
    if act is None or not mint:
        return
    act.note(mint, "checking", detail, symbol=symbol or "", source=detail)


class SolPriceSource:
    name = "sol_price"

    def __init__(self, ctx: Context):
        self.ctx = ctx

    async def fetch(self) -> float:
        key = secret("JUPITER_API_KEY")
        if key:
            r = await self.ctx.http.get("https://api.jup.ag/price/v3", params={"ids": WSOL},
                                        headers={"x-api-key": key}, timeout=10)
            r.raise_for_status()
            return float(r.json()[WSOL]["usdPrice"])
        r = await self.ctx.http.get("https://api.coingecko.com/api/v3/simple/price",
                                    params={"ids": "solana", "vs_currencies": "usd"}, timeout=10)
        r.raise_for_status()
        return float(r.json()["solana"]["usd"])

    async def poll(self) -> None:
        usd = await self.fetch()
        self.ctx.push(self.name, SolPrice(ts=time.time(), source=self.name, usd=usd))

    async def run(self) -> None:
        await poll_loop(self.ctx, self.name, self.ctx.settings.sources.jupiter.sol_price_poll_s, self.poll,
                        Backoff(10, 300))


class Rugcheck:
    """api.rugcheck.xyz report summary for watchlist tokens (optional, off by default)."""
    name = "rugcheck"

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.done: set[str] = set()
        self.bucket = TokenBucket(0.5, 2)

    async def poll(self) -> None:
        if self.ctx.watch is None:
            return
        for mint in [m for m in self.ctx.watch.priority_list(time.time()) if m not in self.done][:5]:
            self.done.add(mint)
            await self.bucket.take()
            r = await self.ctx.http.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary", timeout=15)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            j = r.json()
            self.ctx.push(self.name, Enrichment(ts=time.time(), source="rugcheck", mint=mint, data={
                "rugcheck_score": j.get("score"), "rugcheck_score_normalised": j.get("score_normalised"),
                "rugcheck_risks": [x.get("name") for x in j.get("risks") or []][:10]}))
            _note_check(self.ctx, mint, "rugcheck")

    async def run(self) -> None:
        await poll_loop(self.ctx, self.name, 15, self.poll, Backoff(30, 600))


class Unsupported:
    """Axiom / j7tracker / fomo.family have no official public data API (as of 2026-10).

    * Axiom: only reverse-engineered websockets behind Cloudflare (ToS risk).
    * fomo.family: private API (prod-api.fomo.family); a read-only community SDK exists.
    * j7tracker: its public API is deploy/buy/sell only — no social/X feed.

    To add one anyway, implement a class with `async run()` that pushes
    `ExternalSignal(source=..., mint=..., signal=...)` events — features
    `external_signals` / signal-based rules pick them up automatically.
    """

    def __init__(self, ctx: Context, name: str):
        self.ctx, self.name = ctx, name

    async def run(self) -> None:
        st = self.ctx.stat(self.name)
        st.status, st.note = "unsupported", "no official API — see solmeme/sources/misc.py"
