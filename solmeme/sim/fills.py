"""Paper fill model — pessimistic on purpose.

Every order lands after a sampled latency and is priced against the market
state *at landing* (all real trades in between already applied). It can fail
(random drop, slippage beyond tolerance), pays priority fees on every attempt,
Jito tip + platform fee when it lands, and buys may get sandwiched.

Venues: pump.fun curve (exact reserves from the trade stream, or reserves
derived from price — the curve's k is constant), constant-product AMM after
migration, or an approximate curve for other launchpads.

Our own fills never move the recorded market (ghost trades), so sizes must
stay small relative to liquidity; the sizing cap enforces that.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field

from solmeme.config import FillsCfg
from solmeme.sim import curve
from solmeme.sim.amm import Pool

PUMP_K = curve.INITIAL_V_SOL * curve.INITIAL_V_TOKENS


@dataclass
class Fill:
    ok: bool
    side: str
    reason: str = ""
    sol: float = 0.0          # buy: SOL into the swap; sell: SOL out of the swap (after swap fee)
    tokens: float = 0.0
    price: float = 0.0        # effective SOL per token
    cost_sol: float = 0.0     # priority + tip + platform fee + rent (outside the swap)
    venue: str = ""
    meta: dict = field(default_factory=dict)


def order_rng(seed: int, *parts) -> random.Random:
    """Deterministic per-order RNG: results never depend on other strategies' activity."""
    h = hashlib.sha256("|".join(str(p) for p in (seed, *parts)).encode()).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


STREAM_STALE_S = 20.0      # a trade stream this much older than the latest price no longer pins the reserves
STREAM_GAP = 0.05          # ... once they disagree by more than this


def _stream_stale(st, reserve_price: float) -> bool:
    """Reserves from a trade stream freeze when the stream stops (the pump.fun feed only keeps the top watched
    coins), while GMGN/Dex keep moving price_sol. Oct 10: 16% of paper curve fills landed 10%+ under the
    price the strategy decided on (IMPOSTINU: paper 56 SOL mcap, real 99); those fake-cheap fills averaged
    +21.6% against -11.7% for the rest. Fill at the decided price instead once the stream is behind it."""
    if not getattr(st, "reserves_exact", False) or not reserve_price or not st.price_sol:
        return False
    behind = (st.price_ts or 0) - (getattr(st, "last_trade_ts", None) or 0) > STREAM_STALE_S
    return behind and abs(reserve_price / st.price_sol - 1) > STREAM_GAP


class FillModel:
    def __init__(self, cfg: FillsCfg, calib=None):
        self.cfg = cfg
        self.calib = calib          # sim/calibration.py: real-vs-paper gaps measured by the live dry run

    def latency_s(self, rng: random.Random) -> float:
        lo, hi = self.cfg.latency_ms
        return rng.uniform(lo, hi) / 1000.0

    # ── quotes (no randomness, no network costs) ───────────────────────────
    def _venue(self, st):
        if st.price_sol is None:
            return None, None
        p = st.price_sol
        if not st.migrated:
            if st.v_sol and st.v_tokens:
                kind = "curve" if st.pool == "pump" else "curve_approx"
                if _stream_stale(st, st.v_sol / st.v_tokens):
                    k = st.v_sol * st.v_tokens          # same curve, moved to the price the strategy decided on
                    return kind, (math.sqrt(k * p), math.sqrt(k / p))
                return kind, (st.v_sol, st.v_tokens)
            return "curve_approx", (math.sqrt(PUMP_K * p), math.sqrt(PUMP_K / p))
        pool = st.amm or Pool.seed_from_price(p)
        if st.amm is not None and _stream_stale(st, pool.price):
            pool = Pool(pool.sol, pool.tokens)
            pool.reanchor(p, None)
        return "amm", pool

    def quote_buy(self, st, sol_in: float) -> tuple[float, str] | None:
        kind, v = self._venue(st)
        if kind is None:
            return None
        if kind == "amm":
            return v.buy_out(sol_in, self.cfg.amm_fee_pct(st.mcap_sol)), kind
        tokens, _, _ = curve.buy_out(v[0], v[1], sol_in, self.cfg.curve_fee(st.pool))
        return tokens, kind

    def quote_sell(self, st, tokens: float) -> tuple[float, str] | None:
        kind, v = self._venue(st)
        if kind is None:
            return None
        if kind == "amm":
            return v.sell_out(tokens, self.cfg.amm_fee_pct(st.mcap_sol)), kind
        sol, _, _ = curve.sell_out(v[0], v[1], tokens, self.cfg.curve_fee(st.pool))
        return sol, kind

    def liquidation_value(self, st, tokens: float) -> float | None:
        q = self.quote_sell(st, tokens)
        return q[0] if q else None

    # ── executions ─────────────────────────────────────────────────────────
    def buy(self, st, sol_in: float, ref_price: float, rng: random.Random) -> Fill:
        c = self.cfg
        if rng.random() < c.fail_rate:
            return Fill(False, "buy", "tx_dropped", cost_sol=c.priority_fee_sol)
        q = self.quote_buy(st, sol_in)
        if q is None or q[0] <= 0:
            return Fill(False, "buy", "no_liquidity", cost_sol=c.priority_fee_sol)
        tokens, venue = q
        limit = ref_price * (1 + c.slippage_tolerance_pct / 100)
        eff = sol_in / tokens
        if self.calib is not None:              # what real prices did to fills like this one
            gap = self.calib.buy_gap(rng)
            if gap is None:
                return Fill(False, "buy", "no_route_live", cost_sol=c.priority_fee_sol)
            eff *= 1 + gap
            tokens = sol_in / eff
        if eff > limit:
            return Fill(False, "buy", "slippage", cost_sol=c.priority_fee_sol,
                        meta={"landed_price": eff, "limit": limit})
        bare = c.jito_tip_sol <= 0                      # no MEV protection
        sandwiched = rng.random() < (c.sandwich_prob_unprotected if bare else c.sandwich_prob)
        if sandwiched:
            extra = c.sandwich_extra_unprotected_pct if bare else c.sandwich_extra_pct
            eff = min(limit, eff * (1 + extra / 100))
            tokens = sol_in / eff
        cost = c.priority_fee_sol + c.jito_tip_sol + sol_in * c.platform_fee_pct / 100 + c.ata_rent_sol
        return Fill(True, "buy", "filled", sol=sol_in, tokens=tokens, price=eff, cost_sol=cost,
                    venue=venue, meta={"sandwiched": sandwiched})

    def sell(self, st, tokens: float, ref_price: float, rng: random.Random, force: bool = False) -> Fill:
        c = self.cfg
        if not force and rng.random() < c.fail_rate:
            return Fill(False, "sell", "tx_dropped", cost_sol=c.priority_fee_sol)
        q = self.quote_sell(st, tokens)
        if q is None:
            return Fill(False, "sell", "no_liquidity", cost_sol=c.priority_fee_sol)
        sol_out, venue = q
        if self.calib is not None:
            sol_out *= 1 + self.calib.sell_gap(rng)
        eff = sol_out / tokens if tokens > 0 else 0.0
        limit = ref_price * (1 - c.sell_slippage_tolerance_pct / 100)
        if not force and eff < limit:
            return Fill(False, "sell", "slippage", cost_sol=c.priority_fee_sol,
                        meta={"landed_price": eff, "limit": limit})
        cost = c.priority_fee_sol + c.jito_tip_sol + sol_out * c.platform_fee_pct / 100
        return Fill(True, "sell", "filled", sol=sol_out, tokens=tokens, price=eff, cost_sol=cost, venue=venue)

    def stale_sell(self, st, tokens: float) -> Fill:
        """No fresh price for a long time: assume we dump at a haircut to the last price."""
        c = self.cfg
        px = (st.price_sol or 0.0) * (1 - c.stale_exit_haircut_pct / 100)
        sol_out = tokens * px
        cost = c.priority_fee_sol + c.jito_tip_sol + sol_out * c.platform_fee_pct / 100
        return Fill(True, "sell", "stale_exit", sol=sol_out, tokens=tokens, price=px, cost_sol=cost, venue="stale")
