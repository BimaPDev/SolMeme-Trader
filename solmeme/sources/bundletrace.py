"""Linked and fresh wallets among a coin's top holders, from the chain (public Solana RPC, no key, nothing sent).

Idea from h100envy/crawl-hive (scan-bundles.ts), Oct 11. GMGN's bundler rate is often missing (its API rate-limits
us), so this is our own check for one person spreading a buy over many wallets:

  holders    the biggest wallets by balance, from pump.fun's live trade feed (the free RPC node refuses
             getTokenLargestAccounts, but the bot already knows every wallet's balance on streamed coins)
  funding    for each one, the oldest of its last 1000 signatures (busier wallets are old and active:
             skipped) and who paid in that first transaction: the SOL sender, or the fee payer
  linked     holders that share a funder (or were funded by another holder) are one cluster; a funder
             holding more than EXCHANGE_SOL is an exchange hot wallet and links nobody
  fresh      holders whose wallet is under FRESH_S old

and emits Enrichment(source="bundletrace"). Features: linked_wallets, linked_pct, fresh_holder_pct.
About 2 calls per holder, so it runs one coin at a time, for the coins nearest a buy.
"""

from __future__ import annotations

import logging
import time

from solmeme.core.events import Enrichment
from solmeme.sim import curve
from solmeme.sources.base import Backoff, Context, TokenBucket, poll_loop

log = logging.getLogger("solmeme.bundletrace")

NAME = "bundletrace"
EXCHANGE_SOL = 5_000.0
FRESH_S = 3 * 86400
LAMPORTS = 1_000_000_000


def funder_of(tx: dict | None, wallet: str) -> str | None:
    """Who funded `wallet` in its first transaction: a system transfer / createAccount into it, else the fee payer."""
    if not tx:
        return None
    msg = (tx.get("transaction") or {}).get("message") or {}
    ixs = list(msg.get("instructions") or [])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        ixs += inner.get("instructions") or []
    for ix in ixs:
        if ix.get("program") != "system":
            continue
        p = ix.get("parsed") or {}
        info, typ = p.get("info") or {}, p.get("type")
        if typ in ("transfer", "transferWithSeed") and info.get("destination") == wallet:
            return info.get("source")
        if typ == "createAccount" and info.get("newAccount") == wallet:
            return info.get("source")
    keys = msg.get("accountKeys") or []
    payer = next((k.get("pubkey") for k in keys if isinstance(k, dict) and k.get("signer")), None)
    return payer if payer and payer != wallet else None


def clusters(holders: dict[str, float], funders: dict[str, str], exchanges: set[str]) -> list[dict]:
    """Union holders with their funder; a cluster is a group of 2+ holders. Returns [{wallets, pct, funder}], biggest first."""
    count: dict[str, int] = {}
    for f in funders.values():
        count[f] = count.get(f, 0) + 1
    linkers = {f for f, n in count.items() if (n > 1 or f in holders) and f not in exchanges}
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x
    for w, f in funders.items():
        if f in linkers:
            parent[find(w)] = find(f)
    groups: dict[str, list[str]] = {}
    for w in holders:
        groups.setdefault(find(w), []).append(w)
    out = [dict(wallets=len(ws), pct=round(sum(holders[w] for w in ws), 2), funder=root)
           for root, ws in groups.items() if len(ws) > 1]
    return sorted(out, key=lambda c: -c["pct"])


def bundled(d: dict, min_wallets: int = 5, min_pct: float = 10.0) -> str | None:
    """Why the trace says this is one person's bag spread over wallets (None = not)."""
    n, pct = d.get("linked_wallets") or 0, d.get("linked_pct") or 0.0
    if n >= min_wallets or pct >= min_pct:
        return f"{n} of its top holders were funded by the same wallet ({pct:.0f}% of the supply): a bundle"
    return None


class BundleTrace:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.cfg = ctx.settings.sources.bundletrace
        self.bucket = TokenBucket(self.cfg.max_requests_per_s, capacity=3)
        self.done: dict[str, float] = {}
        self.wallet_cache: dict[str, tuple[str | None, float | None]] = {}   # funding never changes

    async def _rpc(self, method: str, params: list):
        await self.bucket.take()
        r = await self.ctx.http.post(self.cfg.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                     timeout=15)
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"{method}: {body['error'].get('message')}")
        return body.get("result")

    async def first_funding(self, wallet: str) -> tuple[str | None, float | None] | None:
        """(funder, first_ts) or None for an old/busy wallet (1000+ signatures) or one with no history."""
        if wallet in self.wallet_cache:
            return self.wallet_cache[wallet]
        sigs = await self._rpc("getSignaturesForAddress", [wallet, {"limit": 1000}]) or []
        if not sigs or len(sigs) >= 1000:
            return None
        oldest = sigs[-1]
        tx = await self._rpc("getTransaction", [oldest["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        out = (funder_of(tx, wallet), oldest.get("blockTime") or (tx or {}).get("blockTime"))
        self.wallet_cache[wallet] = out
        return out

    async def trace(self, holders: dict[str, float], now: float) -> dict:
        funders, fresh_n, fresh_pct, checked = {}, 0, 0.0, 0
        for w in list(holders)[: self.cfg.max_wallets]:
            try:
                ff = await self.first_funding(w)
            except Exception as e:  # noqa: BLE001 - one wallet must not stop the rest
                log.debug("bundletrace %s: %s", w[:6], e)
                continue
            if ff is None:
                continue
            checked += 1
            funder, first_ts = ff
            if first_ts and now - first_ts < FRESH_S:
                fresh_n += 1
                fresh_pct += holders[w]
            if funder:
                funders[w] = funder
        shared = {f for f in funders.values() if list(funders.values()).count(f) > 1 or f in holders}
        exchanges = set()
        for f in shared:
            try:
                bal = await self._rpc("getBalance", [f])
                if ((bal or {}).get("value") or 0) / LAMPORTS > EXCHANGE_SOL:
                    exchanges.add(f)
            except Exception:  # noqa: BLE001
                continue
        cl = clusters({w: holders[w] for w in funders} | {w: holders[w] for w in holders if w in shared}, funders, exchanges)
        top = cl[0] if cl else {}
        return dict(checked=checked, linked_wallets=top.get("wallets", 0), linked_pct=top.get("pct", 0.0),
                    linked_funder=top.get("funder"), clusters=len(cl), fresh_wallets=fresh_n, fresh_pct=round(fresh_pct, 2),
                    checked_ts=now)

    async def poll(self) -> None:
        holders_fn = getattr(self.ctx, "holders_fn", None)
        if self.ctx.watch is None or holders_fn is None:
            return
        now = time.time()
        for mint in self.ctx.watch.priority_list(now)[: self.cfg.top_n]:
            if now - self.done.get(mint, 0) < self.cfg.recheck_s:
                continue
            bals = holders_fn(mint) or {}
            if len(bals) < 3:
                continue
            top = dict(sorted(bals.items(), key=lambda kv: -kv[1])[: self.cfg.max_wallets])
            holders = {w: 100.0 * b / curve.TOTAL_SUPPLY for w, b in top.items() if b > 0}
            self.done[mint] = now
            d = await self.trace(holders, now)
            self.ctx.push(NAME, Enrichment(ts=time.time(), source=NAME, mint=mint, data=d))
            break                                      # one coin per poll: ~2 calls per holder
        if len(self.done) > 5000:
            self.done = dict(sorted(self.done.items(), key=lambda kv: -kv[1])[:2000])
        if len(self.wallet_cache) > 50_000:
            self.wallet_cache.clear()

    async def run(self) -> None:
        self.ctx.stat(NAME)
        await poll_loop(self.ctx, NAME, self.cfg.poll_s, self.poll, Backoff(20, 600))
