"""Per-token rolling state, updated incrementally from events.

Works with two data tiers:
  * trade stream (PumpPortal tokenTrade): exact curve reserves, per-wallet
    balances (holder concentration, dev sells, sniper share), flow windows;
  * snapshots only (GMGN trenches, DexScreener batch polls — free): price,
    mcap, liquidity, holders, txn counts.
Features return None when their tier's data is absent for a token.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from solmeme.core.events import (
    CurveTrade, Enrichment, ExternalSignal, Migration, PoolTrade, SolPrice, TokenLaunched, WalletTrade,
)
from solmeme.features.narrative import Narratives
from solmeme.sim import curve
from solmeme.sim.amm import Pool

PUMP_K = curve.INITIAL_V_SOL * curve.INITIAL_V_TOKENS  # virtual constant product (fees are taken outside)
# DexScreener dexIds that are still bonding curves (not graduated AMM pools)
CURVE_DEX_IDS = {"pumpfun", "pump", "launchlab", "raydiumlaunchlab", "bonk", "letsbonk", "meteoradbc", "moonshot",
                 "boop", "believe", "bags"}
# GMGN trench `launchpad` → our pool name. Bags, Believe and Jupiter Studio launch on Meteora's
# Dynamic Bonding Curve; pump.fun's mayhem/agent modes are still the pump.fun curve.
LAUNCHPAD_TO_POOL = {
    "Pump.fun": "pump", "pump_mayhem": "pump", "pump_mayhem_agent": "pump", "pump_agent": "pump",
    "meteora_virtual_curve": "meteoradbc", "bags": "meteoradbc", "believe": "meteoradbc", "jup_studio": "meteoradbc",
    "letsbonk": "bonk", "bonkers": "bonk", "ray_launchpad": "launchlab", "boop": "boop",
    "Moonshot": "moonshot", "moonshot_app": "moonshot",
}
DEX_TO_POOL = {"pumpfun": "pump", "pump": "pump", "launchlab": "launchlab", "raydiumlaunchlab": "launchlab",
               "bonk": "bonk", "letsbonk": "bonk", "meteoradbc": "meteoradbc"}
HIST_KEEP_S = 900.0
LIVE_FRESH_S = 30.0       # a streamed swap price this recent beats any polled snapshot
FALLBACK_AFTER_S = 30.0   # GeckoTerminal prices only fill in when DexScreener is this stale
GMGN_LEADS_S = 30.0       # a GMGN curve price this recent beats Dex/Gecko snapshots of a curve token
SNIPER_WINDOW_S = 5.0


@dataclass
class TokenState:
    mint: str
    first_seen_ts: float
    symbol: str = ""
    name: str = ""
    creator: str = ""
    pool: str = "pump"
    pool_src: str = ""                   # launch (PumpPortal) > gmgn (trench launchpad) > dex; "" = default guess
    created_ts: float | None = None
    seq: int = 0

    price_sol: float | None = None
    price_ts: float | None = None
    first_price_sol: float | None = None
    ath_price_sol: float | None = None
    ath_ts: float | None = None
    mcap_sol_reported: float | None = None

    v_sol: float | None = None           # curve reserves (trade stream or derived from price)
    v_tokens: float | None = None
    curve_k: float | None = None         # constant product seen at launch (non-pump curves differ)
    reserves_exact: bool = False         # only while a trade stream keeps the reserves current
    migrated: bool = False
    migrated_ts: float | None = None
    amm: Pool | None = None

    has_trade_stream: bool = False
    trades: deque = field(default_factory=deque)        # (ts, is_buy, sol, trader)
    price_hist: deque = field(default_factory=deque)    # (ts, price_sol)
    n_buys: int = 0
    n_sells: int = 0
    vol_buy: float = 0.0
    vol_sell: float = 0.0
    traders: set = field(default_factory=set)
    buyers: set = field(default_factory=set)
    balances: dict = field(default_factory=dict)        # wallet -> token balance (from newTokenBalance)
    first_buy_ts: dict = field(default_factory=dict)
    dev_peak_balance: float = 0.0
    dev_balance: float = 0.0
    dev_sold: bool = False
    last_trade_ts: float | None = None

    wallet_buys: list = field(default_factory=list)     # (ts, wallet, label)
    banned_hits: set = field(default_factory=set)       # banned wallets that created or bought this token
    wallet_sells: list = field(default_factory=list)

    gmgn: dict = field(default_factory=dict)
    gmgn_ts: float | None = None
    dex: dict = field(default_factory=dict)
    dex_ts: float | None = None
    dev_launch_pct: float | None = None                  # creator's buy in the launch transaction, % of supply
    j7_launched: bool = False                            # metadata hosted by j7tracker: a coin made from a tweet
    live_px_ts: float | None = None                      # last price from a real-time swap stream
    gmgn_px_ts: float | None = None                      # last price from GMGN (curve tokens)
    security: dict = field(default_factory=dict)
    signals: dict = field(default_factory=dict)          # signal name -> first ts
    ext: dict = field(default_factory=dict)              # source name -> latest data (Axiom, fomo, j7…)

    # ── price plumbing ─────────────────────────────────────────────────────
    def set_price(self, ts: float, p: float | None) -> None:
        if p is None or not math.isfinite(p) or p <= 0:
            return
        self.price_sol = p
        self.price_ts = ts
        if self.first_price_sol is None:
            self.first_price_sol = p
        if self.ath_price_sol is None or p > self.ath_price_sol:
            self.ath_price_sol = p
            self.ath_ts = ts
        self.price_hist.append((ts, p))
        while self.price_hist and self.price_hist[0][0] < ts - HIST_KEEP_S:
            self.price_hist.popleft()
        if self.amm is not None and not self.reserves_exact and self.migrated:
            self.amm.reanchor(p, None)
        if not self.migrated and not self.reserves_exact and (self.pool == "pump" or self.curve_k):
            # curve: k is constant, so price pins both virtual reserves
            k = self.curve_k or PUMP_K
            self.v_sol = math.sqrt(k * p)
            self.v_tokens = math.sqrt(k / p)

    def price_at_or_before(self, ts: float) -> float | None:
        best = None
        for t, p in self.price_hist:
            if t <= ts:
                best = p
            else:
                break
        return best

    @property
    def mcap_sol(self) -> float | None:
        if self.price_sol is not None:
            return self.price_sol * curve.TOTAL_SUPPLY
        return self.mcap_sol_reported

    @property
    def age_ref_ts(self) -> float:
        return self.created_ts if self.created_ts is not None else self.first_seen_ts


class MarketState:
    """All token states + global context (SOL price, launch rate)."""

    def __init__(self, excluded: set[str] | None = None, max_tokens: int = 50_000):
        self.banned: set[str] = set()       # lab/banlist.py; live sessions load it, replays leave it empty
        self.ban_roles: dict[str, str] = {} # address -> creator | cluster | funder | operation | manual
        self.whales: set[str] = set()       # lab/whales.py; same
        self.tokens: dict[str, TokenState] = {}
        self.sol_usd: float | None = None
        self.excluded = excluded or set()
        self.max_tokens = max_tokens
        self.launch_ts: deque = deque()
        self.migration_ts: deque = deque()
        self.seen_sigs: set[str] = set()
        self.narratives = Narratives()
        self._sig_order: deque = deque()
        self.by_creator: dict[str, list[str]] = {}   # creator -> mints launched, in order (dev history)

    def get(self, mint: str) -> TokenState | None:
        return self.tokens.get(mint)

    def _token(self, mint: str, ts: float) -> TokenState:
        st = self.tokens.get(mint)
        if st is None:
            st = TokenState(mint=mint, first_seen_ts=ts)
            self.tokens[mint] = st
            if len(self.tokens) > self.max_tokens:
                self._evict(ts)
        return st

    def _evict(self, now: float) -> None:
        # drop the stalest quarter (by last price update / first seen)
        items = sorted(self.tokens.values(), key=lambda s: s.price_ts or s.first_seen_ts)
        for st in items[: len(items) // 4]:
            del self.tokens[st.mint]

    def _dup(self, sig: str) -> bool:
        if not sig:
            return False
        if sig in self.seen_sigs:
            return True
        self.seen_sigs.add(sig)
        self._sig_order.append(sig)
        if len(self._sig_order) > 200_000:
            self.seen_sigs.discard(self._sig_order.popleft())
        return False

    @staticmethod
    def _polled_price(st: TokenState, ts: float, p: float | None) -> None:
        if st.live_px_ts is not None and ts - st.live_px_ts < LIVE_FRESH_S:
            return                  # snapshots lag the swap stream: don't step the price backwards
        st.set_price(ts, p)

    CLUSTER_EARLY_S = 180.0

    def _ban_counts(self, st: TokenState, wallet: str, ts: float) -> bool:
        """A banned creator, funder or operator always taints a coin. A banned copy-trade cluster only does when it
        buys in the first 3 minutes (sniping or bundling the launch): piling into a coin that is already running
        is what such bots do to every runner (Oct 10: three cluster wallets bought SOBDOG at +7.5 min, already 8x,
        and blocked it for every strategy on its way to 22x)."""
        if self.ban_roles.get(wallet, "cluster") != "cluster":
            return True
        return ts - st.age_ref_ts <= self.CLUSTER_EARLY_S

    @staticmethod
    def _gmgn_leads(st: TokenState, ts: float) -> bool:
        """Dex and Gecko lag on curve tokens: Dex can show the launch price for a minute while the coin
        triples. Mixed in with GMGN's fresh prices, that stale print looked like a 60% dip under the
        ATH, and holder_dip learned to buy it (paper filled at the stale price, live paid +100%)."""
        return not st.migrated and st.gmgn_px_ts is not None and ts - st.gmgn_px_ts <= GMGN_LEADS_S

    def _usd_to_sol(self, usd) -> float | None:
        try:
            usd = float(usd)
        except (TypeError, ValueError):
            return None
        if not self.sol_usd or usd <= 0:
            return None
        return usd / self.sol_usd

    # ── apply ──────────────────────────────────────────────────────────────
    def apply(self, ev) -> TokenState | None:
        """Update state from one event. Returns the touched token (if any)."""
        k = ev.kind
        if k == "sol_price":
            self.sol_usd = ev.usd
            return None
        if k == "headline":
            self.narratives.add_headline(ev)
            return None
        mint = getattr(ev, "mint", None)
        if not mint or mint in self.excluded:
            return None
        if k == "curve_trade" and self._dup(ev.sig):
            return None
        st = self._token(mint, ev.ts)
        st.seq += 1
        if k == "launch":
            self._on_launch(st, ev)
        elif k == "curve_trade":
            self._on_curve_trade(st, ev)
        elif k == "pool_trade":
            self._on_pool_trade(st, ev)
        elif k == "migration":
            self._on_migration(st, ev)
        elif k == "wallet_trade":
            (st.wallet_buys if ev.side == "buy" else st.wallet_sells).append((ev.ts, ev.wallet, ev.label))
            if ev.side == "buy" and ev.wallet in self.banned and self._ban_counts(st, ev.wallet, ev.ts):
                st.banned_hits.add(ev.wallet)
            if ev.symbol and not st.symbol:
                st.symbol = ev.symbol
        elif k == "enrichment":
            self._on_enrichment(st, ev)
        elif k == "signal":
            st.signals.setdefault(ev.signal, ev.ts)
            if ev.data:
                self._on_enrichment(st, Enrichment(ts=ev.ts, source=ev.source, mint=mint, data=ev.data))
        return st

    def _on_launch(self, st: TokenState, ev: TokenLaunched) -> None:
        st.created_ts = ev.ts
        st.symbol, st.name, st.creator, st.pool = ev.symbol, ev.name, ev.creator, ev.pool
        st.pool_src = "launch"
        st.j7_launched = "j7track" in (ev.uri or "")
        st.dev_launch_pct = (ev.initial_buy_tokens or 0.0) / curve.TOTAL_SUPPLY * 100
        self.narratives.add_launch(ev.ts, ev.mint, ev.name, ev.symbol)
        if ev.creator and ev.creator in self.banned:
            st.banned_hits.add(ev.creator)
        if ev.creator:
            launches = self.by_creator.setdefault(ev.creator, [])
            if ev.mint not in launches:
                launches.append(ev.mint)
                del launches[:-50]
        self.launch_ts.append(ev.ts)
        while self.launch_ts and self.launch_ts[0] < ev.ts - 600:
            self.launch_ts.popleft()
        if ev.v_sol and ev.v_tokens:
            # One snapshot, not a feed: without a trade stream, later Dex/GMGN prices must move
            # the reserves too, or fills and bonding_pct stay pinned at the launch price.
            st.curve_k = ev.v_sol * ev.v_tokens
            st.set_price(ev.ts, curve.price(ev.v_sol, ev.v_tokens))
        if ev.initial_buy_tokens and ev.creator:
            st.balances[ev.creator] = ev.initial_buy_tokens
            st.dev_peak_balance = st.dev_balance = ev.initial_buy_tokens
            st.first_buy_ts[ev.creator] = ev.ts

    def _on_curve_trade(self, st: TokenState, ev: CurveTrade) -> None:
        st.has_trade_stream = True
        st.v_sol, st.v_tokens, st.reserves_exact = ev.v_sol, ev.v_tokens, True
        st.set_price(ev.ts, curve.price(ev.v_sol, ev.v_tokens))
        self._record_trade(st, ev.ts, ev.side == "buy", ev.sol, ev.trader, ev.new_balance, ev.tokens)

    def _on_pool_trade(self, st: TokenState, ev: PoolTrade) -> None:
        st.has_trade_stream = True
        if not st.migrated:
            st.migrated, st.migrated_ts = True, ev.ts
        if st.amm is None and st.price_sol:
            st.amm = Pool.seed_from_price(st.price_sol)
        if st.amm is not None:
            st.amm.apply_trade(ev.side, ev.sol, ev.tokens)
        if ev.tokens > 0:
            st.set_price(ev.ts, ev.sol / ev.tokens)
        self._record_trade(st, ev.ts, ev.side == "buy", ev.sol, ev.trader, getattr(ev, "new_balance", None), ev.tokens)

    def _record_trade(self, st: TokenState, ts: float, is_buy: bool, sol: float, trader: str,
                      new_balance: float | None, tokens: float | None = None) -> None:
        st.trades.append((ts, is_buy, sol, trader))
        while st.trades and st.trades[0][0] < ts - HIST_KEEP_S:
            st.trades.popleft()
        st.last_trade_ts = ts
        st.traders.add(trader)
        if is_buy:
            st.n_buys += 1
            st.vol_buy += sol
            st.buyers.add(trader)
            st.first_buy_ts.setdefault(trader, ts)
        else:
            st.n_sells += 1
            st.vol_sell += sol
        if new_balance is None and trader and tokens:
            # pump.fun's own feed doesn't send the wallet's new balance (PumpPortal did): add up its trades instead.
            # Oct 11: without this every streamed coin showed ~1 holder, so holder/top-10/creator share and the
            # bundle trace had nothing. Wallets that bought before we kept the coin are missed: shares are a floor.
            new_balance = max(0.0, st.balances.get(trader, 0.0) + (tokens if is_buy else -tokens))
        if new_balance is not None:
            if new_balance > 0:
                st.balances[trader] = new_balance
            else:
                st.balances.pop(trader, None)
        if trader and trader == st.creator:
            bal = new_balance if new_balance is not None else st.dev_balance
            st.dev_balance = bal
            st.dev_peak_balance = max(st.dev_peak_balance, bal)
            if not is_buy:
                st.dev_sold = True

    def _on_migration(self, st: TokenState, ev: Migration) -> None:
        st.migrated, st.migrated_ts = True, ev.ts
        st.reserves_exact = False
        self.migration_ts.append(ev.ts)
        while self.migration_ts and self.migration_ts[0] < ev.ts - 3600:
            self.migration_ts.popleft()
        if st.price_sol:
            st.amm = Pool.seed_from_price(st.price_sol)

    def _on_enrichment(self, st: TokenState, ev: Enrichment) -> None:
        d = ev.data
        src = ev.source
        if src.startswith("gmgn"):
            st.gmgn.update({k: v for k, v in d.items() if v is not None})
            st.gmgn_ts = ev.ts
            if d.get("created_ts") and st.created_ts is None:
                st.created_ts = float(d["created_ts"])
            if d.get("symbol") and not st.symbol:
                st.symbol = d["symbol"]
            if d.get("migrated") and not st.migrated:
                st.migrated, st.migrated_ts = True, ev.ts
            venue = LAUNCHPAD_TO_POOL.get(d.get("launchpad") or "")
            if venue and st.pool_src in ("", "dex"):
                st.pool, st.pool_src = venue, "gmgn"    # GMGN-only tokens: don't price a Meteora curve as pump.fun
            if not st.has_trade_stream:
                p = self._usd_to_sol(d.get("price_usd"))
                if p is None and d.get("mcap_usd"):
                    m = self._usd_to_sol(d.get("mcap_usd"))
                    p = m / curve.TOTAL_SUPPLY if m else None
                self._polled_price(st, ev.ts, p)
                if p:
                    st.gmgn_px_ts = ev.ts
        elif src.startswith("dex"):
            st.dex.update({k: v for k, v in d.items() if v is not None})
            st.dex_ts = ev.ts
            if d.get("symbol") and not st.symbol:
                st.symbol = d["symbol"]
            dex_id = d.get("dex_id")
            if dex_id and not st.pool_src and not st.has_trade_stream and dex_id in DEX_TO_POOL:
                st.pool, st.pool_src = DEX_TO_POOL[dex_id], "dex"   # no launch event seen: learn the venue from Dex
            if dex_id and dex_id not in CURVE_DEX_IDS and not st.migrated:
                st.migrated, st.migrated_ts = True, ev.ts
            if (not st.has_trade_stream or st.migrated) and not self._gmgn_leads(st, ev.ts):
                p = d.get("price_sol")
                if p is None:
                    p = self._usd_to_sol(d.get("price_usd"))
                self._polled_price(st, ev.ts, p)
            if st.migrated and d.get("liquidity_sol") and st.price_sol:
                if st.amm is None:
                    st.amm = Pool.seed_from_price(st.price_sol)
                st.amm.reanchor(st.price_sol, float(d["liquidity_sol"]))
        elif src == "solstream":
            p = d.get("price_sol") or self._usd_to_sol(d.get("price_usd"))
            if p:
                st.set_price(ev.ts, p)
                st.live_px_ts = ev.ts
        elif src == "gecko":
            st.ext.setdefault(src, {}).update({k: v for k, v in d.items() if v is not None})
            if d.get("symbol") and not st.symbol:
                st.symbol = d["symbol"]
            dex_stale = st.dex_ts is None or ev.ts - st.dex_ts > FALLBACK_AFTER_S
            if dex_stale and (not st.has_trade_stream or st.migrated) and not self._gmgn_leads(st, ev.ts):
                self._polled_price(st, ev.ts, d.get("price_sol") or self._usd_to_sol(d.get("price_usd")))
        elif src == "pump_callout":                  # pump.fun Hot callouts: kept per callout (sources/pumpfun_social.py)
            calls = st.ext.setdefault("pump_callouts", {})
            calls[d.get("callout_id") or str(len(calls))] = {**d, "ts": ev.ts}
            if len(calls) > 50:
                for k in sorted(calls, key=lambda k: calls[k]["ts"])[:-50]:
                    calls.pop(k)
        elif src.startswith("rugcheck") or src.startswith("security"):
            st.security.update(d)
        else:
            st.ext.setdefault(src, {}).update({k: v for k, v in d.items() if v is not None})
