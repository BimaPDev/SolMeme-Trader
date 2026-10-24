"""SolanaStreaming websocket (wss://api.solanastreaming.com, X-API-KEY header).

One swapSubscribe carries every filter (OR): `baseTokenMint` for the tokens we hold or watch,
`walletAccount` for tracked wallets and top whales. Swaps land ~1 s after the chain, so held
tokens get real-time prices for stops and exits instead of 5-10 s old DexScreener snapshots.

Each swap becomes an Enrichment(source="solstream") price tick; it is not a full trade stream
(we only see tokens since we subscribed), so trade-count features stay on their own sources.
Plans are metered per message: `daily_messages` caps the day, after which only held tokens stay.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import websockets

from solmeme.config import secret
from solmeme.core.events import Enrichment, WalletTrade
from solmeme.sources.base import Backoff, Context

log = logging.getLogger("solmeme.solstream")
NAME = "solstream"
WSOL = "So11111111111111111111111111111111111111112"
USD_QUOTES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def parse_swap(msg: dict, ts: float, mints: set[str], wallets: set[str], sol_usd: float | None) -> list:
    """swap notification → price tick for watched mints, WalletTrade for tracked wallets (pure)."""
    sw = msg.get("swap") or {}
    mint, quote = sw.get("baseTokenMint"), sw.get("quoteTokenMint")
    side = sw.get("swapType")
    if not mint or mint == WSOL or side not in ("buy", "sell"):
        return []
    px, usd = _f(sw.get("quotePrice")), _f(sw.get("usdValue"))
    data: dict = {"side": side, "usd": usd, "wallet": sw.get("walletAccount") or "", "sig": msg.get("signature") or ""}
    if quote == WSOL:
        data["price_sol"] = px
    elif quote in USD_QUOTES:
        data["price_usd"] = px
    else:
        return []
    out: list = []
    if mint in mints and px:
        out.append(Enrichment(ts=ts, source=NAME, mint=mint, data={k: v for k, v in data.items() if v is not None}))
    w = data["wallet"]
    if w in wallets:
        out.append(WalletTrade(ts=ts, source=NAME, wallet=w, label="tracked", mint=mint, side=side, usd=usd,
                               sol=usd / sol_usd if usd and sol_usd else None, tx=data["sig"]))
    return out


class SolStream:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.cfg = ctx.settings.sources.solstream
        self.key = secret("SOLANASTREAMING_API_KEY")
        self.mints: set[str] = set()          # what the live subscription currently filters on
        self.wallets: set[str] = set()
        self.sub_id = None
        self._ws = None
        self._next_id = 1
        self._day = ""
        self.messages_today = 0
        self.budget_exhausted = False

    def _want(self) -> tuple[set[str], set[str]]:
        watch = self.ctx.watch
        held = set(watch.pinned) if watch is not None else set()
        if self.budget_exhausted:
            return held, set()
        ordered = watch.priority_list(time.time()) if watch is not None else []
        mints = set(list(dict.fromkeys([*sorted(held), *ordered]))[: max(self.cfg.max_mints, len(held))])
        wallets = set(self.ctx.settings.sources.tracked_wallets) if self.cfg.wallets else set()
        return mints, wallets

    def _count(self) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day != self._day:
            self._day, self.messages_today, self.budget_exhausted = day, 0, False
        self.messages_today += 1
        if self.cfg.daily_messages and self.messages_today >= self.cfg.daily_messages and not self.budget_exhausted:
            self.budget_exhausted = True
            log.warning("solstream: %d messages today, keeping only held tokens", self.messages_today)

    def _include(self, mints: set[str], wallets: set[str]) -> dict:
        inc: dict = {}
        if mints:
            inc["baseTokenMint"] = sorted(mints)
        if wallets:
            inc["walletAccount"] = sorted(wallets)
        return inc

    async def _rpc(self, method: str, params: dict) -> int:
        rid = self._next_id
        self._next_id += 1
        await self._ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
        return rid

    async def _sync(self) -> None:
        """Keep the one swap subscription's filters in step with what we hold and watch."""
        while True:
            await asyncio.sleep(3)
            if self._ws is None:
                continue
            mints, wallets = self._want()
            if (mints, wallets) == (self.mints, self.wallets) or not (mints or wallets):
                continue
            try:
                inc = self._include(mints, wallets)
                if self.sub_id is None:
                    await self._rpc("swapSubscribe", {"include": inc})
                else:
                    await self._rpc("updateSubscriptionParams", {"subscription_id": self.sub_id, "update": {"include": inc}})
                self.mints, self.wallets = mints, wallets
                self.ctx.stat(NAME).note = f"{len(mints)} tokens · {len(wallets)} wallets · {self.messages_today} msgs today"
            except Exception as exc:  # noqa: BLE001
                log.debug("solstream filter update failed: %r", exc)

    async def run(self) -> None:
        st = self.ctx.stat(NAME)
        if not self.key:
            st.status, st.note = "off", "set SOLANASTREAMING_API_KEY to stream real-time swap prices"
            return
        bo = Backoff(base_s=5, max_s=600)
        syncer = asyncio.create_task(self._sync())
        try:
            while True:
                try:
                    async with websockets.connect(self.cfg.url, additional_headers={"X-API-KEY": self.key},
                                                  ping_interval=20, ping_timeout=20, max_size=2 ** 22) as ws:
                        self._ws, self.sub_id = ws, None
                        self.mints, self.wallets = set(), set()
                        st.status, st.note = "ok", "connected"
                        bo.ok()
                        async for raw in ws:
                            try:
                                msg = json.loads(raw)
                            except ValueError:
                                continue
                            if "error" in msg:
                                st.note = str(msg["error"])[:200]
                                log.warning("solstream: %s", st.note)
                                continue
                            res = msg.get("result")
                            if isinstance(res, dict) and res.get("subscription_id") is not None:
                                self.sub_id = res["subscription_id"]
                                continue
                            if "swap" not in msg:
                                continue
                            self._count()
                            for ev in parse_swap(msg, time.time(), self.mints, self.wallets, None):
                                self.ctx.push(NAME, ev)
                except Exception as exc:  # noqa: BLE001 - a source must never kill the run
                    self._ws = None
                    wait = bo.fail(repr(exc))
                    st.errors += 1
                    st.status, st.note = "reconnecting", f"{type(exc).__name__}: {str(exc)[:120]}; retry in {wait:.0f}s"
                    log.warning("solstream disconnected: %r (reconnect in %.0fs)", exc, wait)
                    await asyncio.sleep(wait)
        finally:
            syncer.cancel()
