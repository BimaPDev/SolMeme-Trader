"""DexScreener (free, no key): batch price/liquidity/txn snapshots for the
watchlist (30 mints per request, 300 req/min limit) and paid boosts/profiles
as discrete signals. Port of PaperTrader fetchers/dexscreener.py."""

from __future__ import annotations

import asyncio
import time

from solmeme.core.events import Enrichment, ExternalSignal
from solmeme.sources.base import Backoff, Context, TokenBucket, poll_loop

NAME = "dexscreener"
BASE = "https://api.dexscreener.com"
HEADERS = {"Accept": "application/json", "User-Agent": "SolMemeTrader/0.1"}
SOL_SYMBOLS = {"SOL", "WSOL"}


def _note_check(ctx, mint: str, detail: str, symbol: str = "") -> None:
    act = getattr(ctx, "activity", None)
    if act is None or not mint:
        return
    act.note(mint, "checking", detail, symbol=symbol or "", source=detail)


def _f(x):
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


def normalize_pair(p: dict) -> tuple[str, dict]:
    base = p.get("baseToken") or {}
    quote = p.get("quoteToken") or {}
    liq = p.get("liquidity") or {}
    tx5 = (p.get("txns") or {}).get("m5") or {}
    vol = p.get("volume") or {}
    chg = p.get("priceChange") or {}
    info = p.get("info") or {}
    socials = {s.get("type"): s.get("url") for s in info.get("socials") or [] if isinstance(s, dict)}
    quote_is_sol = (quote.get("symbol") or "").upper() in SOL_SYMBOLS
    d = {
        "symbol": base.get("symbol"),
        "dex_id": p.get("dexId"),
        "pair": p.get("pairAddress"),
        "price_usd": _f(p.get("priceUsd")),
        "price_sol": _f(p.get("priceNative")) if quote_is_sol else None,
        "liquidity_usd": _f(liq.get("usd")),
        "liquidity_sol": (_f(liq.get("quote")) or 0) * 2 if quote_is_sol and _f(liq.get("quote")) else None,
        "buys_m5": _f(tx5.get("buys")),
        "sells_m5": _f(tx5.get("sells")),
        "volume_m5_usd": _f(vol.get("m5")),
        "volume_h1_usd": _f(vol.get("h1")),
        "price_chg_m5": _f(chg.get("m5")),
        "price_chg_h1": _f(chg.get("h1")),
        "mcap_usd": _f(p.get("marketCap") or p.get("fdv")),
        "twitter": socials.get("twitter"),
        "telegram": socials.get("telegram"),
        "website": (info.get("websites") or [{}])[0].get("url") if info.get("websites") else None,
    }
    return base.get("address") or "", {k: v for k, v in d.items() if v is not None}


class DexScreener:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.cfg = ctx.settings.sources.dexscreener
        self.bucket = TokenBucket(self.cfg.max_requests_per_min / 60.0, capacity=10)
        self.seen: set[tuple[str, str]] = set()

    async def _get(self, path: str):
        await self.bucket.take()
        r = await self.ctx.http.get(BASE + path, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()

    async def poll_watch(self) -> None:
        if self.ctx.watch is None:
            return
        mints = self.ctx.watch.priority_list(time.time())
        max_batches = max(1, int(self.cfg.max_requests_per_min / 60 * self.cfg.watch_poll_s))
        for i in range(0, min(len(mints), 30 * max_batches), 30):
            chunk = mints[i:i + 30]
            pairs = await self._get("/tokens/v1/solana/" + ",".join(chunk)) or []
            best: dict[str, dict] = {}
            for p in pairs if isinstance(pairs, list) else []:
                if p.get("chainId") != "solana":
                    continue
                mint, d = normalize_pair(p)
                if mint and (mint not in best or (d.get("liquidity_usd") or 0) > (best[mint].get("liquidity_usd") or 0)):
                    best[mint] = d
            now = time.time()
            for mint, d in best.items():
                self.ctx.push(NAME, Enrichment(ts=now, source="dexscreener", mint=mint, data=d))
                _note_check(self.ctx, mint, "dex price", symbol=str(d.get("symbol") or ""))

    async def poll_boosts(self) -> None:
        for path, sig in (("/token-boosts/latest/v1", "dex_boost"), ("/token-profiles/latest/v1", "dex_profile")):
            rows = await self._get(path) or []
            now = time.time()
            for r in rows if isinstance(rows, list) else []:
                if r.get("chainId") != "solana" or not r.get("tokenAddress"):
                    continue
                key = (r["tokenAddress"], sig)
                if key in self.seen:
                    continue
                self.seen.add(key)
                self.ctx.push(NAME, ExternalSignal(ts=now, source="dexscreener", mint=r["tokenAddress"], signal=sig,
                                                   data={"boost_amount": r.get("totalAmount")} if sig == "dex_boost" else {}))
                if self.ctx.watch is not None:
                    self.ctx.watch.touch(r["tokenAddress"], "boost", now)

    async def run(self) -> None:
        self.ctx.stat(NAME)
        await asyncio.gather(
            poll_loop(self.ctx, NAME, self.cfg.watch_poll_s, self.poll_watch, Backoff(10, 300)),
            poll_loop(self.ctx, NAME, self.cfg.boosts_poll_s, self.poll_boosts, Backoff(30, 600)),
        )
