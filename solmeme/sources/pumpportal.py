"""PumpPortal websocket (wss://pumpportal.fun/api/data).

  subscribeNewToken / subscribeMigration — free (API key required since 2026-05-01)
  subscribeTokenTrade [mints]            — billed per event (≈0.01 SOL / 10k events)
  subscribeAccountTrade [wallets]        — billed per event

Only ONE connection is allowed per key/IP (repeat connections get banned for an
hour), so a single socket carries every subscription. Trade subscriptions are
driven by the watchlist and capped by a daily event budget.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import websockets

from solmeme.config import secret
from solmeme.core.events import CurveTrade, Migration, PoolTrade, TokenLaunched, WalletTrade
from solmeme.sources.base import Backoff, Context

log = logging.getLogger("solmeme.pumpportal")
NAME = "pumpportal"
CURVE_POOLS = {"pump", "bonk", "launchlab"}


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def parse_message(msg: dict, ts: float, tracked: set[str]) -> list:
    """PumpPortal JSON → normalized events (pure; unit-tested on captured payloads)."""
    tx = msg.get("txType")
    mint = msg.get("mint")
    if not tx or not mint:
        return []
    pool = msg.get("pool") or "pump"
    if tx == "create":
        return [TokenLaunched(
            ts=ts, source=NAME, mint=mint, name=msg.get("name") or "", symbol=msg.get("symbol") or "",
            creator=msg.get("traderPublicKey") or "", pool=pool,
            v_sol=_f(msg.get("vSolInBondingCurve"), None), v_tokens=_f(msg.get("vTokensInBondingCurve"), None),
            mcap_sol=_f(msg.get("marketCapSol"), None), initial_buy_sol=_f(msg.get("solAmount")),
            initial_buy_tokens=_f(msg.get("initialBuy")), uri=msg.get("uri") or "",
        )]
    if tx == "migrate" or tx == "migration":
        return [Migration(ts=ts, source=NAME, mint=mint, pool=pool)]
    if tx not in ("buy", "sell"):
        return []
    trader = msg.get("traderPublicKey") or ""
    sol, tokens = _f(msg.get("solAmount")), _f(msg.get("tokenAmount"))
    out: list = []
    v_sol, v_tok = msg.get("vSolInBondingCurve"), msg.get("vTokensInBondingCurve")
    if pool in CURVE_POOLS and v_sol is not None and v_tok is not None:
        out.append(CurveTrade(
            ts=ts, source=NAME, mint=mint, trader=trader, side=tx, sol=sol, tokens=tokens,
            new_balance=_f(msg.get("newTokenBalance"), None), v_sol=_f(v_sol), v_tokens=_f(v_tok),
            mcap_sol=_f(msg.get("marketCapSol"), None), pool=pool, sig=msg.get("signature") or "",
        ))
    else:
        out.append(PoolTrade(ts=ts, source=NAME, mint=mint, trader=trader, side=tx, sol=sol, tokens=tokens,
                             pool=pool, mcap_sol=_f(msg.get("marketCapSol"), None), sig=msg.get("signature") or ""))
    if trader in tracked:
        out.append(WalletTrade(ts=ts, source=NAME, wallet=trader, label="tracked", mint=mint, side=tx, sol=sol,
                               tx=msg.get("signature") or ""))
    return out


class PumpPortal:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        cfg = ctx.settings
        self.url = cfg.sources.pumpportal.url
        key = secret("PUMPPORTAL_API_KEY")
        if key:
            self.url += ("&" if "?" in self.url else "?") + f"api-key={key}"
        self.tracked = set(cfg.sources.tracked_wallets)
        self.max_subs = cfg.record.max_subscriptions
        self.daily_budget = cfg.record.trade_stream_daily_events
        self.subscribed: set[str] = set()
        self.desired: set[str] = set()
        self.pinned: set[str] = set()
        self._ws = None
        self._day = ""
        self.trade_events_today = 0
        self.budget_exhausted = False
        self.trade_streams_unavailable = False

    def want(self, mints: list[str], pinned: set[str]) -> None:
        """Called by the live runner with the priority-ordered watchlist."""
        self.pinned = set(pinned)
        if self.trade_streams_unavailable:
            self.desired = set()
        elif self.budget_exhausted:
            self.desired = set(pinned)
        else:
            self.desired = set(list(dict.fromkeys([*pinned, *mints]))[: self.max_subs])

    def _count_trade(self) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day != self._day:
            self._day, self.trade_events_today, self.budget_exhausted = day, 0, False
        self.trade_events_today += 1
        if self.daily_budget and self.trade_events_today >= self.daily_budget and not self.budget_exhausted:
            self.budget_exhausted = True
            self.desired = set(self.pinned)
            log.warning("PumpPortal trade-event budget (%d/day) reached; keeping only held tokens subscribed",
                        self.daily_budget)

    async def _send(self, method: str, keys: list[str] | None = None) -> None:
        if self._ws is None:
            return
        payload = {"method": method}
        if keys is not None:
            payload["keys"] = keys
        await self._ws.send(json.dumps(payload))

    async def _sync_subs(self) -> None:
        while True:
            await asyncio.sleep(2)
            if self._ws is None:
                continue
            add = list(self.desired - self.subscribed)
            drop = list(self.subscribed - self.desired)
            try:
                for i in range(0, len(drop), 100):
                    await self._send("unsubscribeTokenTrade", drop[i:i + 100])
                for i in range(0, len(add), 100):
                    await self._send("subscribeTokenTrade", add[i:i + 100])
                self.subscribed = (self.subscribed - set(drop)) | set(add)
            except Exception as exc:  # noqa: BLE001
                log.debug("sub sync failed: %r", exc)

    async def run(self) -> None:
        st = self.ctx.stat(NAME)
        bo = Backoff(base_s=5, max_s=300)
        syncer = asyncio.create_task(self._sync_subs())
        try:
            while True:
                try:
                    async with websockets.connect(self.url, ping_interval=20, ping_timeout=20,
                                                  max_size=2 ** 22) as ws:
                        self._ws = ws
                        st.status, st.note = "ok", "connected"
                        await self._send("subscribeNewToken")
                        await self._send("subscribeMigration")
                        if self.tracked:
                            await self._send("subscribeAccountTrade", sorted(self.tracked))
                        self.subscribed = set()
                        bo.ok()
                        async for raw in ws:
                            try:
                                msg = json.loads(raw)
                            except ValueError:
                                continue
                            if "errors" in msg or "error" in msg:
                                st.note = str(msg.get("errors") or msg.get("error"))[:200]
                                log.warning("pumpportal: %s", st.note)
                                continue
                            if "message" in msg and "txType" not in msg:
                                text = str(msg["message"])
                                if "only available when connecting with an API key" in text:
                                    # tokenTrade/accountTrade need a key funded with >= 0.02 SOL
                                    self.trade_streams_unavailable = True
                                    self.desired = set()
                                    st.note = "launches+migrations only (trade streams need a funded API key)"
                                    log.warning("pumpportal: %s", text)
                                continue
                            evs = parse_message(msg, time.time(), self.tracked)
                            for ev in evs:
                                if ev.kind in ("curve_trade", "pool_trade"):
                                    self._count_trade()
                                self.ctx.push(NAME, ev)
                except Exception as exc:  # noqa: BLE001
                    self._ws = None
                    wait = bo.fail(repr(exc))
                    st.errors += 1
                    st.status, st.note = "reconnecting", f"{type(exc).__name__}: {str(exc)[:120]}"
                    log.warning("pumpportal disconnected: %r (reconnect in %.0fs)", exc, wait)
                    await asyncio.sleep(wait)
        finally:
            syncer.cancel()
