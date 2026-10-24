"""GeckoTerminal public API (free, no key, 30 calls/min).

  trending pools  → ExternalSignal "gecko_trending" (feature `gecko_trending`) with the pool's
                    price, market cap and 5 min buyers/sellers
  tokens/multi    → backup prices for held and top watchlist tokens; token_state only uses them
                    while DexScreener is stale, so they cover Dex outages without fighting it
"""

from __future__ import annotations

import asyncio
import time

from solmeme.core.events import Enrichment, ExternalSignal
from solmeme.sources.base import Backoff, Context, TokenBucket, poll_loop

NAME = "gecko"
BASE = "https://api.geckoterminal.com/api/v2/networks/solana"
HEADERS = {"Accept": "application/json;version=20230302", "User-Agent": "SolMemeTrader/0.1"}
WSOL = "So11111111111111111111111111111111111111112"


def _f(x):
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


def _addr(rel: dict, key: str) -> str:
    rid = ((rel.get(key) or {}).get("data") or {}).get("id") or ""
    return rid.split("_", 1)[1] if rid.startswith("solana_") else ""


def parse_trending(body: dict) -> list[tuple[str, dict]]:
    out = []
    for p in body.get("data") or []:
        a, rel = p.get("attributes") or {}, p.get("relationships") or {}
        mint = _addr(rel, "base_token")
        if not mint or mint == WSOL:
            continue
        tx5 = ((a.get("transactions") or {}).get("m5")) or {}
        d = {"price_sol": _f(a.get("base_token_price_native_currency")) if _addr(rel, "quote_token") == WSOL else None,
             "price_usd": _f(a.get("base_token_price_usd")), "mcap_usd": _f(a.get("market_cap_usd") or a.get("fdv_usd")),
             "buyers_m5": tx5.get("buyers"), "sellers_m5": tx5.get("sellers"),
             "price_chg_m5": _f((a.get("price_change_percentage") or {}).get("m5")),
             "dex": ((rel.get("dex") or {}).get("data") or {}).get("id")}
        out.append((mint, {k: v for k, v in d.items() if v is not None}))
    return out


def parse_tokens(body: dict) -> list[tuple[str, dict]]:
    out = []
    for t in body.get("data") or []:
        a = t.get("attributes") or {}
        if not a.get("address"):
            continue
        d = {"symbol": a.get("symbol"), "price_usd": _f(a.get("price_usd")),
             "mcap_usd": _f(a.get("market_cap_usd") or a.get("fdv_usd")),
             "reserve_usd": _f(a.get("total_reserve_in_usd")), "volume_h24_usd": _f((a.get("volume_usd") or {}).get("h24"))}
        out.append((a["address"], {k: v for k, v in d.items() if v is not None}))
    return out


class Gecko:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.cfg = ctx.settings.sources.gecko
        self.bucket = TokenBucket(self.cfg.max_requests_per_min / 60.0, capacity=3)

    async def _get(self, path: str) -> dict:
        await self.bucket.take()
        r = await self.ctx.http.get(BASE + path, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()

    async def poll_trending(self) -> None:
        now = time.time()
        for mint, d in parse_trending(await self._get("/trending_pools?page=1")):
            self.ctx.push(NAME, ExternalSignal(ts=now, source=NAME, mint=mint, signal="gecko_trending", data=d))
            if self.ctx.watch is not None:
                self.ctx.watch.touch(mint, "signal", now)

    async def poll_watch(self) -> None:
        if self.ctx.watch is None:
            return
        mints = self.ctx.watch.priority_list(time.time())[:30]
        if not mints:
            return
        now = time.time()
        for mint, d in parse_tokens(await self._get("/tokens/multi/" + ",".join(mints))):
            self.ctx.push(NAME, Enrichment(ts=now, source=NAME, mint=mint, data=d))

    async def run(self) -> None:
        self.ctx.stat(NAME)
        await asyncio.gather(
            poll_loop(self.ctx, NAME, self.cfg.trending_poll_s, self.poll_trending, Backoff(30, 900)),
            poll_loop(self.ctx, NAME, self.cfg.watch_poll_s, self.poll_watch, Backoff(20, 600)),
        )
