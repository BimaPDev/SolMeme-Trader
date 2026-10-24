"""Our own rug check, read straight from the chain (public Solana RPC, no key, nothing sent).

For the coins the bot is closest to buying (top of the watchlist, and held ones) it reads:

  mint account        mint authority (more can be printed), freeze authority (holders can be frozen: the
                      Solana honeypot), and Token-2022 extensions that trap sellers (transfer fee, transfer
                      hook, permanent delegate, non-transferable, accounts frozen by default)
  largest accounts    top-1 / top-10 share of supply held by people; accounts whose owner is a program
                      (the bonding curve, an AMM vault) are left out. The free public node refuses this
                      call, so it runs only with HELIUS_API_KEY (free tier) in .env; mint flags work without

and emits Enrichment(source="chaincheck"). It runs in shadow: the features (chain_*) are recorded for every
check so they can be compared with GMGN's flags and with outcomes before any rule uses them.
"""

from __future__ import annotations

import asyncio
import logging
import time

from solmeme.config import secret
from solmeme.core.events import Enrichment
from solmeme.sources.base import Backoff, Context, TokenBucket, poll_loop

log = logging.getLogger("solmeme.chaincheck")

NAME = "chaincheck"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TRAP_EXTENSIONS = {"transferFeeConfig": "transfer fee", "transferHook": "transfer hook",
                   "permanentDelegate": "permanent delegate", "nonTransferable": "non-transferable",
                   "defaultAccountState": "accounts frozen by default", "pausableConfig": "pausable"}


# ── creator history straight from pump.fun's program (idea from h100envy/crawl-hive, Oct 11) ──────────────
# A pump.fun bonding-curve account stores its creator (bytes 49..81) and a "complete" flag (byte 48, graduated).
# One getProgramAccounts with a memcmp on the creator lists every coin they ever launched, which our own
# history (only launches the bot saw) misses: Zoroark's dev had 21 launches, 2 graduated.
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
_DEV_CACHE: dict[str, tuple[float, dict]] = {}
DEV_CACHE_S = 1800


async def creator_history(http, creator: str, rpc_url: str = PUBLIC_RPC) -> dict:
    """{launches, graduated, wallet_txs, wallet_age_h} for a pump.fun creator (cached 30 min); {} on failure."""
    hit = _DEV_CACHE.get(creator)
    if hit and time.time() - hit[0] < DEV_CACHE_S:
        return hit[1]
    import base64

    async def rpc(method, params):
        r = await http.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"{method}: {body['error'].get('message')}")
        return body.get("result")
    try:
        curves = await rpc("getProgramAccounts", [PUMP_PROGRAM, {"encoding": "base64", "dataSlice": {"offset": 48, "length": 1},
                                                                 "filters": [{"memcmp": {"offset": 49, "bytes": creator}}]}]) or []
        sigs = await rpc("getSignaturesForAddress", [creator, {"limit": 1000}]) or []
    except Exception as e:  # noqa: BLE001 - no history is not a verdict
        log.debug("creator history %s: %s", creator[:6], e)
        return {}
    grad = sum(1 for a in curves if base64.b64decode(a["account"]["data"][0])[:1] == b"\x01")
    oldest = sigs[-1].get("blockTime") if sigs and len(sigs) < 1000 else None
    d = dict(launches=len(curves), graduated=grad, wallet_txs=len(sigs),
             wallet_age_h=round((time.time() - oldest) / 3600, 1) if oldest else None)
    _DEV_CACHE[creator] = (time.time(), d)
    if len(_DEV_CACHE) > 5000:
        for k in sorted(_DEV_CACHE, key=lambda k: _DEV_CACHE[k][0])[:2500]:
            _DEV_CACHE.pop(k, None)
    return d


def serial_launcher(h: dict, min_launches: int = 10, max_grad_rate: float = 0.10) -> str | None:
    """Why this creator looks like a coin factory (None = not)."""
    n, g = h.get("launches") or 0, h.get("graduated") or 0
    if n >= min_launches and g / n < max_grad_rate:
        return f"the creator launched {n} coins on pump.fun and only {g} graduated"
    return None


def mint_flags(value: dict | None) -> dict:
    """From getAccountInfo(mint, jsonParsed): authorities, token program and trap extensions."""
    if not value:
        return {}
    info = ((value.get("data") or {}).get("parsed") or {}).get("info") or {}
    traps = []
    for ext in info.get("extensions") or []:
        name = ext.get("extension")
        if name == "defaultAccountState" and (ext.get("state") or {}).get("accountState") != "frozen":
            continue
        if name == "transferFeeConfig":
            bps = ((ext.get("state") or {}).get("newerTransferFee") or {}).get("transferFeeBasisPoints") or 0
            if not bps:
                continue
        if name in TRAP_EXTENSIONS:
            traps.append(TRAP_EXTENSIONS[name])
    return {"mint_authority": bool(info.get("mintAuthority")), "freeze_authority": bool(info.get("freezeAuthority")),
            "token_2022": value.get("owner") == TOKEN_2022, "traps": traps,
            "supply": float(info.get("supply") or 0) / 10 ** int(info.get("decimals") or 0) if info.get("supply") else None}


def holder_shares(largest: list[dict], owners: dict[str, str], owner_programs: dict[str, str | None],
                  supply: float | None) -> dict:
    """Top-1 / top-10 % of supply held by wallets that are people (owned by the System program)."""
    if not supply:
        return {}
    people = []
    for a in largest:
        wallet = owners.get(a.get("address"))
        prog = owner_programs.get(wallet) if wallet else None
        if wallet and prog not in (None, SYSTEM_PROGRAM):
            continue                                   # a program's account: curve, pool vault, locker
        people.append(float(a.get("uiAmount") or 0))
    people.sort(reverse=True)
    return {"top1_pct": round(100 * people[0] / supply, 2) if people else 0.0,
            "top10_pct": round(100 * sum(people[:10]) / supply, 2), "holders_seen": len(people)}


def reasons(d: dict, top1_max: float = 50, top10_max: float = 80) -> list[str]:
    out = []
    if d.get("freeze_authority"):
        out.append("freeze authority")
    if d.get("mint_authority"):
        out.append("mint authority")
    out += [f"token-2022 {t}" for t in d.get("traps") or []]
    if (d.get("top1_pct") or 0) > top1_max:
        out.append(f"top1 {d['top1_pct']:.0f}%")
    if (d.get("top10_pct") or 0) > top10_max:
        out.append(f"top10 {d['top10_pct']:.0f}%")
    return out


class ChainCheck:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.cfg = ctx.settings.sources.chaincheck
        self.bucket = TokenBucket(self.cfg.max_requests_per_s, capacity=4)
        self.mint_done: dict[str, dict] = {}           # mint flags never need a second look
        self.creators: dict[str, str | None] = {}
        self.holders_at: dict[str, float] = {}
        self.url_i = 0

    def _urls(self) -> list[str]:
        key = secret("HELIUS_API_KEY")
        return [f"https://mainnet.helius-rpc.com/?api-key={key}", *self.cfg.rpc_urls] if key else list(self.cfg.rpc_urls)

    async def _rpc(self, method: str, params: list):
        await self.bucket.take()
        urls = self._urls()
        url = urls[self.url_i % len(urls)]
        r = await self.ctx.http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
        if r.status_code == 429:
            self.url_i += 1                            # next public node
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"{method}: {body['error'].get('message')}")
        return body.get("result")

    async def check(self, mint: str, now: float) -> dict:
        flags = self.mint_done.get(mint)
        if flags is None:
            res = await self._rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
            flags = mint_flags((res or {}).get("value"))
            if flags:
                self.mint_done[mint] = flags
        d = {k: v for k, v in flags.items() if k != "supply"}
        if mint.endswith("pump"):
            dev = await self._creator(mint)
            if dev:
                h = await creator_history(self.ctx.http, dev)
                d.update({f"dev_{k}": v for k, v in h.items()})
        if secret("HELIUS_API_KEY"):
            d.update(await self._holders(mint, flags.get("supply")))
        d["reasons"] = reasons(d, self.cfg.top1_max_pct, self.cfg.top10_max_pct)
        d["checked_ts"] = now
        return d

    async def _creator(self, mint: str) -> str | None:
        if mint in self.creators:
            return self.creators[mint]
        try:
            r = await self.ctx.http.get("https://advanced-indexer.pump.fun/in-memory-coin/" + mint, timeout=10,
                                        headers={"User-Agent": "Mozilla/5.0", "Origin": "https://pump.fun"})
            dev = (r.json() or {}).get("dev") if r.status_code == 200 else None
        except Exception:  # noqa: BLE001
            dev = None
        self.creators[mint] = dev
        return dev

    async def _holders(self, mint: str, supply: float | None) -> dict:
        largest = ((await self._rpc("getTokenLargestAccounts", [mint])) or {}).get("value") or []
        addrs = [a["address"] for a in largest if a.get("address")]
        owners: dict[str, str] = {}
        if addrs:
            accs = ((await self._rpc("getMultipleAccounts", [addrs, {"encoding": "jsonParsed"}])) or {}).get("value") or []
            for a, acc in zip(addrs, accs):
                o = ((((acc or {}).get("data") or {}).get("parsed") or {}).get("info") or {}).get("owner")
                if o:
                    owners[a] = o
        wallets = sorted(set(owners.values()))
        progs: dict[str, str | None] = {}
        if wallets:
            accs = ((await self._rpc("getMultipleAccounts", [wallets, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}]))
                    or {}).get("value") or []
            progs = {w: (acc or {}).get("owner") for w, acc in zip(wallets, accs)}
        return holder_shares(largest, owners, progs, supply)

    async def poll(self) -> None:
        if self.ctx.watch is None:
            return
        now = time.time()
        holders = bool(secret("HELIUS_API_KEY"))     # without holder shares there is nothing to re-check
        due = [m for m in self.ctx.watch.priority_list(now)[: self.cfg.top_n]
               if (m not in self.holders_at if not holders else now - self.holders_at.get(m, 0) >= self.cfg.recheck_s)]
        for mint in due[: self.cfg.per_poll]:
            try:
                d = await self.check(mint, now)
            except Exception as e:            # one bad coin must not stop the rest
                log.debug("chaincheck %s: %s", mint, e)
                self.holders_at[mint] = now   # don't hammer a coin that errors
                continue
            self.holders_at[mint] = now
            self.ctx.push(NAME, Enrichment(ts=time.time(), source=NAME, mint=mint, data=d))
        if len(self.holders_at) > 5000:
            self.holders_at = dict(sorted(self.holders_at.items(), key=lambda kv: -kv[1])[:2000])
            self.mint_done = {m: self.mint_done[m] for m in self.holders_at if m in self.mint_done}

    async def run(self) -> None:
        self.ctx.stat(NAME)
        await poll_loop(self.ctx, NAME, self.cfg.poll_s, self.poll, Backoff(20, 600))
