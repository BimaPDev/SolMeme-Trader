"""pump.fun's own boards: Trending ("movers") and Hot callouts, read from the JSON its website loads.

  advanced-indexer.pump.fun/boards/trending    the Trending board: rank, volume, trades, KOL count per coin
  frontend-api-v3.pump.fun/home-feed           Hot callouts: a pump.fun user posts a coin they hold, with the
                                               market cap they called it at, the multiple since, their thesis,
                                               their position and PnL, and how many callouts they have made
  frontend-api-v3.pump.fun/pnl-leaderboard     who the callers are: daily/weekly top traders by PnL

Every coin seen becomes an ExternalSignal (pump_trending / pump_callout); token_state keeps the callouts per
coin (features pump_callouts, pump_callout_leader, pump_trending_rank...) and the coins go on the watchlist.
Read-only; a few requests a minute.
"""

from __future__ import annotations

import time

from solmeme.core.events import ExternalSignal
from solmeme.sources.base import Backoff, Context, poll_loop

NAME = "pumpfun_social"
TRENDING = "https://advanced-indexer.pump.fun/boards/trending"
FEED = "https://frontend-api-v3.pump.fun/home-feed"
LEADERS = "https://frontend-api-v3.pump.fun/pnl-leaderboard"
COIN = "https://advanced-indexer.pump.fun/in-memory-coin/"      # per coin: holder rewards, cashback, mayhem, charity, live
HEADERS = {"User-Agent": "Mozilla/5.0", "Origin": "https://pump.fun", "Referer": "https://pump.fun/", "Accept": "application/json"}


def _f(x):
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


def parse_trending(body: dict) -> list[tuple[str, dict]]:
    out = []
    for rank, e in enumerate((body or {}).get("entries") or [], 1):
        mint = e.get("m") or ""
        if not mint or not str(e.get("c", "solana")).startswith("solana"):
            continue
        out.append((mint, {k: v for k, v in dict(
            rank=rank, symbol=e.get("t"), mcap_usd=_f(e.get("mc")), ath_usd=_f(e.get("ath")), kol=e.get("kol"),
            vol_usd_5m=_f(e.get("vUsd5")), vol_usd_1h=_f(e.get("vUsd1h")), tx_5m=e.get("tx5"), age_s=e.get("age")).items()
            if v is not None}))
    return out


def parse_callouts(body: dict, leaders: dict[str, int]) -> list[tuple[str, dict]]:
    out = []
    for c in (body or {}).get("coins") or []:
        mint, pos = c.get("coinMint") or "", c.get("position") or {}
        call = pos.get("callout") or {}
        if not mint or not call.get("calloutId") or c.get("chain") not in (None, "solana"):
            continue
        wallet = pos.get("walletAddress") or ""
        out.append((mint, {k: v for k, v in dict(
            callout_id=call["calloutId"], caller=pos.get("userName") or "", caller_wallet=wallet,
            caller_rank=leaders.get(wallet), caller_callouts=pos.get("totalCallouts"),
            called_mcap_usd=_f(call.get("calledOutAtMcap")), multiple=_f(call.get("multiple")),
            thesis=(call.get("thesis") or "")[:200], likes=call.get("likes"), called_ts=call.get("calloutTimestamp"),
            position_usd=_f(pos.get("valueUsd")), cost_usd=_f(pos.get("costBasisUsd")), pnl_usd=_f(pos.get("pnlUsd")),
            feed_rank=c.get("recommendationRank"), symbol=c.get("symbol")).items() if v not in (None, "")}))
    return out


class PumpFunSocial:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.cfg = ctx.settings.sources.pumpfun_social
        self.leaders: dict[str, int] = {}
        self.seen_calls: set[str] = set()
        self.flags_done: set[str] = set()

    async def _get(self, url: str, params: dict) -> dict:
        r = await self.ctx.http.get(url, params=params, headers=HEADERS, timeout=20)
        r.raise_for_status()
        return r.json()

    def _push(self, mint: str, signal: str, data: dict, now: float) -> None:
        self.ctx.push(NAME, ExternalSignal(ts=now, source=signal, mint=mint, signal=signal, data=data))
        if self.ctx.watch is not None:
            self.ctx.watch.touch(mint, "signal", now)

    async def poll_trending(self) -> None:
        now = time.time()
        body = await self._get(TRENDING, {"tier": "web", "surface": "TRENDING", "platform": "WEB", "limit": self.cfg.trending_limit})
        for mint, d in parse_trending(body):
            self._push(mint, "pump_trending", {**d, "seen_ts": now}, now)

    async def poll_callouts(self) -> None:
        now = time.time()
        body = await self._get(FEED, {"pageSize": self.cfg.feed_size, "chain": "all", "platform": "WEB"})
        for mint, d in parse_callouts(body, self.leaders):
            if d["callout_id"] in self.seen_calls:
                continue                              # one event per callout; its PnL is re-read on the coin card
            self.seen_calls.add(d["callout_id"])
            self._push(mint, "pump_callout", d, now)
        if len(self.seen_calls) > 50_000:
            self.seen_calls.clear()

    async def poll_leaders(self) -> None:
        board: dict[str, int] = {}
        for period in ("weekly", "daily"):
            body = await self._get(LEADERS, {"period": period, "sort": "combined", "limit": 100})
            for e in (body or {}).get("entries") or []:
                w = e.get("walletAddress")
                if w and (e.get("pnlSol") or 0) > 0:
                    board[w] = min(board.get(w, 10_000), int(e.get("rank") or 10_000))
        if board:
            self.leaders = board

    async def poll_flags(self) -> None:
        """pump.fun's own flags for the coins nearest a buy, read once each (they don't change)."""
        if self.ctx.watch is None:
            return
        now = time.time()
        todo = [m for m in self.ctx.watch.priority_list(now)[:40] if m.endswith("pump") and m not in self.flags_done][:5]
        for mint in todo:
            self.flags_done.add(mint)
            try:
                d = await self._get(COIN + mint, {})
            except Exception:  # noqa: BLE001 - one coin missing must not stop the rest
                continue
            flags = {k: bool(d.get(src)) for k, src in (("holder_reward", "isHolderReward"), ("cashback", "isCashbackEnabled"),
                                                       ("mayhem", "isMayhemMode"), ("charity", "isCharity"), ("live", "isCurrentlyLive"))}
            # Oct 11: recorded to test before any rule. An image on pump.fun's own CDN means the coin was made on the
            # site; a raw IPFS link usually means a launch script. Reuse = how many other coins carry the same link.
            img = str(d.get("imageUrl") or "")
            if d.get("athMarketCapUsd"):
                flags["ath_mcap_usd"] = float(d["athMarketCapUsd"])   # pump.fun's own high, from before we saw the coin
            if d.get("quoteMint"):
                flags["quote_sol"] = d["quoteMint"] in ("So11111111111111111111111111111111111111112",
                                                        "11111111111111111111111111111111")
            flags.update(image=bool(img), image_cdn="imagedelivery.net" in img,
                         social_reuse=max(int(d.get(f"{k}ReuseCount") or 0) for k in ("twitter", "telegram", "website")))
            self.ctx.push(NAME, ExternalSignal(ts=now, source="pump_coin", mint=mint, signal="pump_coin", data=flags))
        if len(self.flags_done) > 50_000:
            self.flags_done.clear()

    async def run(self) -> None:
        import asyncio

        self.ctx.stat(NAME)
        await asyncio.gather(
            poll_loop(self.ctx, NAME, self.cfg.leaders_poll_s, self.poll_leaders, Backoff(60, 1800)),
            poll_loop(self.ctx, NAME, self.cfg.trending_poll_s, self.poll_trending, Backoff(30, 900)),
            poll_loop(self.ctx, NAME, self.cfg.callouts_poll_s, self.poll_callouts, Backoff(30, 900)),
            poll_loop(self.ctx, NAME, 10, self.poll_flags, Backoff(30, 900)),
        )
