"""GMGN.ai official OpenAPI (https://openapi.gmgn.ai) — async port of
PaperTrader fetchers/gmgn.py.

Auth: X-APIKEY header + `timestamp` and `client_id` query params. Key is free
at https://gmgn.ai/ai. The website itself is Cloudflare-protected and its ToS
forbids scraping — use only this documented API.

Differences from the PaperTrader version (on purpose):
  * no ingest-time filtering (rug ratio, mcap, age…) — filtering belongs in
    strategies, otherwise backtests inherit a hidden selection bias;
  * no server-side "safe" trench preset for the same reason;
  * every response becomes normalized events stamped with receive time.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from solmeme.config import secret
from solmeme.core.events import Enrichment, ExternalSignal, WalletTrade
from solmeme.sources.base import Backoff, Context, TokenBucket, poll_loop

log = logging.getLogger("solmeme.gmgn")
NAME = "gmgn"
HOST = "https://openapi.gmgn.ai"
TRENCHES_PATH = "/v1/trenches"
WALLET_ACTIVITY_PATH = "/v1/user/wallet_activity"
TOKEN_INFO_PATH = "/v1/token/info"
TOKEN_SECURITY_PATH = "/v1/token/security"
SMARTMONEY_PATH = "/v1/user/smartmoney"
KOL_PATH = "/v1/user/kol"

SOL_PLATFORMS = [
    "Pump.fun", "pump_mayhem", "pump_mayhem_agent", "pump_agent",
    "letsbonk", "bonkers", "bags", "memoo", "liquid", "bankr", "zora",
    "surge", "anoncoin", "moonshot_app", "wendotdev", "heaven", "sugar",
    "token_mill", "believe", "trendsfun", "trends_fun", "jup_studio",
    "Moonshot", "boop", "ray_launchpad", "meteora_virtual_curve", "xstocks",
]
SOL_QUOTE_ADDRESS_TYPES = [4, 5, 3, 1, 13, 0]
RESPONSE_KEY = {"new_creation": "new_creation", "near_completion": "pump", "completed": "completed"}
SIGNAL_FOR_TYPE = {"new_creation": "gmgn_new", "near_completion": "gmgn_almost", "completed": "gmgn_migrated"}


def _note_check(ctx, mint: str, detail: str, symbol: str = "") -> None:
    act = getattr(ctx, "activity", None)
    if act is None or not mint:
        return
    act.note(mint, "checking", detail, symbol=symbol or "", source=detail)


def available() -> bool:
    return bool(secret("GMGN_API_KEY"))


def _f(x):
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


def _pct(x):
    x = _f(x)
    if x is None:
        return None
    return x * 100.0 if x <= 1.0 else x


def _ts(x):
    x = _f(x)
    if x is None:
        return None
    return x / 1000.0 if x > 1e12 else x


def normalize_trench(item: dict, req_type: str) -> tuple[str, dict] | None:
    mint = item.get("address") or ""
    if not mint:
        return None
    data = {
        "symbol": item.get("symbol"),
        "name": item.get("name"),
        "price_usd": _f(item.get("price")),
        "mcap_usd": _f(item.get("usd_market_cap") or item.get("market_cap")),
        "holders": _f(item.get("holder_count")),
        "top10_pct": _pct(item.get("top_10_holder_rate")),
        "dev_hold_pct": _pct(item.get("dev_team_hold_rate") or item.get("creator_balance_rate")),
        "rug_ratio": _f(item.get("rug_ratio")),
        "bundler_rate": _f(item.get("bundler_rate") or item.get("top_bundler_trader_percentage")
                           or item.get("bundler_trader_amount_rate")),
        "insider_rate": _f(item.get("insider_ratio") or item.get("rat_trader_amount_rate")
                           or item.get("suspected_insider_hold_rate")),
        "sniper_count": _f(item.get("sniper_count")),
        "smart_degen_count": _f(item.get("smart_degen_count")),
        "kol_count": _f(item.get("renowned_count")),
        "volume_1h_usd": _f(item.get("volume_1h")),
        "liquidity_usd": _f(item.get("liquidity")),
        "swaps_1h": _f(item.get("swaps_1h")),
        "twitter": item.get("twitter") or None,
        "telegram": item.get("telegram") or None,
        "website": item.get("website") or None,
        "created_ts": _ts(item.get("created_timestamp")),
        "launchpad": item.get("launchpad_platform"),
        "migrated": req_type == "completed" or bool(item.get("complete_timestamp")),
        "trench": req_type,
    }
    return mint, {k: v for k, v in data.items() if v is not None}


def normalize_activity(item: dict, label: str) -> WalletTrade | None:
    token = item.get("token") or item.get("base_token") or {}
    token = token if isinstance(token, dict) else {}
    mint = item.get("base_address") or item.get("token_address") or token.get("address") or ""
    side = (item.get("event_type") or item.get("type") or item.get("side") or "").lower()
    side = {"transferin": "buy", "transfer_in": "buy", "add": "buy",
            "transferout": "sell", "transfer_out": "sell", "remove": "sell"}.get(side, side)
    if not mint or side not in ("buy", "sell"):
        return None
    return WalletTrade(
        ts=0.0, source=f"gmgn_{label}", wallet=item.get("maker") or item.get("wallet_address") or "unknown",
        label=label, mint=mint, side=side,
        usd=_f(item.get("cost_usd") or item.get("amount_usd") or item.get("buy_cost_usd")),
        symbol=token.get("symbol") or item.get("symbol") or "",
        tx=item.get("transaction_hash") or item.get("tx_hash") or item.get("hash") or "",
    )


class Gmgn:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.cfg = ctx.settings.sources.gmgn
        self.bucket = TokenBucket(self.cfg.rate_per_s, capacity=max(2.0, self.cfg.rate_per_s))
        self.seen_signal: set[tuple[str, str]] = set()
        self.seen_tx: set[str] = set()
        self.security_done: set[str] = set()
        self._started = time.time()

    # ── http ──
    async def _req(self, method: str, path: str, params: dict, body: dict | None = None):
        await self.bucket.take()
        key = secret("GMGN_API_KEY")
        q = {"timestamp": int(time.time()), "client_id": str(uuid.uuid4()), **params}
        headers = {"X-APIKEY": key, "Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "SolMemeTrader/0.1"}
        resp = await self.ctx.http.request(method, HOST + path, params=q, json=body, headers=headers, timeout=25)
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("code") not in (0, None):
            raise RuntimeError(f"GMGN error code={payload.get('code')} msg={payload.get('message') or payload.get('error')}")
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    # ── trenches ──
    async def poll_trenches(self) -> None:
        section = {
            "filters": ["offchain", "onchain"], "launchpad_platform_v2": True, "limit": self.cfg.trench_limit,
            "launchpad_platform": SOL_PLATFORMS, "quote_address_type": SOL_QUOTE_ADDRESS_TYPES,
        }
        body: dict = {"version": "v2", **{t: dict(section) for t in self.cfg.trench_types}}
        data = await self._req("POST", TRENCHES_PATH, {"chain": "sol"}, body) or {}
        now = time.time()
        for req_type in self.cfg.trench_types:
            for item in (data.get(RESPONSE_KEY.get(req_type, req_type)) or []) if isinstance(data, dict) else []:
                if not isinstance(item, dict):
                    continue
                norm = normalize_trench(item, req_type)
                if not norm:
                    continue
                mint, d = norm
                sig = SIGNAL_FOR_TYPE[req_type]
                if (mint, sig) not in self.seen_signal:
                    self.seen_signal.add((mint, sig))
                    self.ctx.push(NAME, ExternalSignal(ts=now, source="gmgn_trenches", mint=mint, signal=sig))
                self.ctx.push(NAME, Enrichment(ts=now, source="gmgn_trenches", mint=mint, data=d))
                if self.ctx.watch is not None:
                    self.ctx.watch.touch(mint, "trench", now)
        if len(self.seen_signal) > 300_000:
            self.seen_signal.clear()

    # ── wallet feeds (public smart money / KOL) ──
    async def poll_feed(self, kind: str) -> None:
        path = SMARTMONEY_PATH if kind == "smartmoney" else KOL_PATH
        data = await self._req("GET", path, {"chain": "sol", "limit": 50}) or {}
        rows = data.get("list") or data.get("activities") or [] if isinstance(data, dict) else data
        self._emit_activity(rows, kind)

    async def poll_tracked(self) -> None:
        for w in self.ctx.settings.sources.tracked_wallets:
            data = await self._req("GET", WALLET_ACTIVITY_PATH,
                                   {"chain": "sol", "wallet_address": w, "limit": 20, "type": ["buy", "sell"]}) or {}
            rows = (data.get("activities") or data.get("list") or []) if isinstance(data, dict) else data
            self._emit_activity(rows, "tracked")

    def _emit_activity(self, rows, label: str) -> None:
        now = time.time()
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            ev = normalize_activity(item, label)
            if ev is None:
                continue
            key = ev.tx or f"{ev.wallet}:{ev.mint}:{ev.side}:{item.get('timestamp')}"
            if key in self.seen_tx:
                continue
            self.seen_tx.add(key)
            # ignore stale backfill: only trades that happened within the last 2 minutes are "news"
            t = _ts(item.get("timestamp") or item.get("block_timestamp"))
            if t is not None and now - t > 120:
                continue
            ev.ts = now
            self.ctx.push(NAME, ev)
            if self.ctx.watch is not None and ev.side == "buy":
                self.ctx.watch.touch(ev.mint, "kol", now)
        if len(self.seen_tx) > 100_000:
            self.seen_tx.clear()

    # ── security for watchlist tokens ──
    async def poll_security(self) -> None:
        if self.ctx.watch is None:
            return
        todo = [m for m in self.ctx.watch.priority_list(time.time()) if m not in self.security_done]
        for mint in todo[: max(1, self.cfg.security_max_per_min // 6)]:
            self.security_done.add(mint)
            sec = await self._req("GET", TOKEN_SECURITY_PATH, {"chain": "sol", "address": mint}) or {}
            if not isinstance(sec, dict):
                continue
            hp = sec.get("is_honeypot")
            d = {
                "honeypot": hp in (True, 1, "1", "yes", "true") or sec.get("honeypot") in (1, "1", True),
                "blacklist": sec.get("is_blacklist") in (True, 1, "1", "yes", "true"),
                "rug_ratio": _f(sec.get("rug_ratio")),
                "top10_pct": _pct(sec.get("top_10_holder_rate")),
                "bundler_rate": _f(sec.get("bundler_trader_amount_rate")),
                "insider_rate": _f(sec.get("suspected_insider_hold_rate") or sec.get("rat_trader_amount_rate")),
                "renounced_mint": sec.get("renounced_mint"),
                "renounced_freeze": sec.get("renounced_freeze_account"),
            }
            self.ctx.push(NAME, Enrichment(ts=time.time(), source="gmgn_security", mint=mint,
                                           data={k: v for k, v in d.items() if v is not None}))
            _note_check(self.ctx, mint, "gmgn security", symbol=str(sec.get("symbol") or ""))

    async def run(self) -> None:
        st = self.ctx.stat(NAME)
        if not available():
            st.status, st.note = "disabled", "GMGN_API_KEY not set"
            return
        c = self.cfg
        tasks = [poll_loop(self.ctx, NAME, c.trenches_poll_s, self.poll_trenches, Backoff(10, 600))]
        for kind in c.wallet_feeds:
            tasks.append(poll_loop(self.ctx, NAME, c.wallet_feed_poll_s, lambda k=kind: self.poll_feed(k),
                                   Backoff(15, 600)))
        if self.ctx.settings.sources.tracked_wallets:
            tasks.append(poll_loop(self.ctx, NAME, c.tracked_wallet_poll_s, self.poll_tracked, Backoff(15, 600)))
        if c.security_max_per_min > 0:
            tasks.append(poll_loop(self.ctx, NAME, 10, self.poll_security, Backoff(15, 600)))
        await asyncio.gather(*tasks)
