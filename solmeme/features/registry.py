"""Feature registry — the single vocabulary for strategy rules, journal
snapshots, pattern mining and the AI reviewer's prompt.

Each feature: fn(state, now, market) -> value | None. None means "unknown"
(data tier not available for this token) and makes any rule on it fail.
Features may only read state built from events with ts <= now.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from solmeme.features import rug
from solmeme.sim import curve


@dataclass(frozen=True)
class Feature:
    name: str
    group: str
    unit: str
    description: str
    fn: Callable[[Any, float, Any], Any]


FEATURES: dict[str, Feature] = {}


def feature(name: str, group: str, unit: str, description: str):
    def deco(fn):
        FEATURES[name] = Feature(name, group, unit, description, fn)
        return fn
    return deco


def _f(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _window(st, now: float, w: float):
    lo = now - w
    out = []
    for t in reversed(st.trades):
        if t[0] < lo:
            break
        if t[0] <= now:
            out.append(t)
    return out


def _price_change(st, now: float, w: float):
    if st.price_sol is None:
        return None
    past = st.price_at_or_before(now - w)
    if past is None or past <= 0:
        return None
    return (st.price_sol / past - 1) * 100


# ── identity / time ────────────────────────────────────────────────────────
@feature("age_s", "time", "s", "Seconds since token creation (or first seen if creation unknown)")
def _age(st, now, m):
    return now - st.age_ref_ts


@feature("is_pump", "time", "bool", "Launched on pump.fun bonding curve")
def _is_pump(st, now, m):
    return st.pool == "pump"


@feature("migrated", "time", "bool", "Token has graduated from the bonding curve to an AMM pool")
def _migrated(st, now, m):
    return st.migrated


@feature("since_migration_s", "time", "s", "Seconds since migration (None if not migrated)")
def _since_mig(st, now, m):
    return now - st.migrated_ts if st.migrated and st.migrated_ts else None


@feature("secs_since_price", "time", "s", "Seconds since the last price observation (staleness)")
def _since_px(st, now, m):
    return now - st.price_ts if st.price_ts else None


# ── price / curve ──────────────────────────────────────────────────────────
@feature("mcap_sol", "price", "SOL", "Market cap in SOL (price x 1B supply)")
def _mcap(st, now, m):
    return st.mcap_sol


@feature("mcap_usd", "price", "USD", "Market cap in USD")
def _mcap_usd(st, now, m):
    return st.mcap_sol * m.sol_usd if st.mcap_sol and m.sol_usd else None


@feature("bonding_pct", "price", "%", "pump.fun bonding-curve progress (100 = graduation); None after migration")
def _bonding(st, now, m):
    if st.migrated or st.pool != "pump" or st.v_tokens is None:
        return None
    return curve.bonding_pct(st.v_tokens)


@feature("price_chg_30s_pct", "price", "%", "Price change over the last 30s")
def _pc30(st, now, m):
    return _price_change(st, now, 30)


@feature("price_chg_60s_pct", "price", "%", "Price change over the last 60s")
def _pc60(st, now, m):
    return _price_change(st, now, 60)


@feature("price_chg_300s_pct", "price", "%", "Price change over the last 5 min")
def _pc300(st, now, m):
    return _price_change(st, now, 300)


@feature("x_from_first_price", "price", "x", "Current price / first observed price")
def _xfirst(st, now, m):
    if st.price_sol and st.first_price_sol:
        return st.price_sol / st.first_price_sol
    return None


@feature("dd_from_ath_pct", "price", "%", "Drawdown from all-time-high observed price")
def _dd(st, now, m):
    if st.price_sol and st.ath_price_sol:
        return (1 - st.price_sol / st.ath_price_sol) * 100
    return None


@feature("secs_since_ath", "price", "s", "Seconds since the all-time-high price")
def _since_ath(st, now, m):
    return now - st.ath_ts if st.ath_ts else None


# ── flow (trade stream only) ───────────────────────────────────────────────
def _flow(w: int):
    def buys(st, now, m):
        return sum(1 for t in _window(st, now, w) if t[1]) if st.has_trade_stream else None

    def sells(st, now, m):
        return sum(1 for t in _window(st, now, w) if not t[1]) if st.has_trade_stream else None

    def buy_vol(st, now, m):
        return sum(t[2] for t in _window(st, now, w) if t[1]) if st.has_trade_stream else None

    def net_flow(st, now, m):
        if not st.has_trade_stream:
            return None
        return sum(t[2] if t[1] else -t[2] for t in _window(st, now, w))

    def uniq(st, now, m):
        return len({t[3] for t in _window(st, now, w) if t[1]}) if st.has_trade_stream else None

    def ratio(st, now, m):
        if not st.has_trade_stream:
            return None
        win = _window(st, now, w)
        b = sum(t[2] for t in win if t[1])
        s = sum(t[2] for t in win if not t[1])
        return b / s if s > 0 else (b if b > 0 else None)

    def max_buy(st, now, m):
        if not st.has_trade_stream:
            return None
        return max((t[2] for t in _window(st, now, w) if t[1]), default=0.0)

    feature(f"buys_{w}s", "flow", "count", f"Buy count in the last {w}s (trade stream)")(buys)
    feature(f"sells_{w}s", "flow", "count", f"Sell count in the last {w}s (trade stream)")(sells)
    feature(f"buy_vol_{w}s", "flow", "SOL", f"Buy volume in SOL in the last {w}s (trade stream)")(buy_vol)
    feature(f"net_flow_{w}s", "flow", "SOL", f"Buy SOL minus sell SOL in the last {w}s (trade stream)")(net_flow)
    feature(f"unique_buyers_{w}s", "flow", "count", f"Distinct buying wallets in the last {w}s (trade stream)")(uniq)
    feature(f"buy_sell_ratio_{w}s", "flow", "ratio", f"Buy SOL / sell SOL in the last {w}s (trade stream)")(ratio)
    feature(f"max_buy_{w}s", "flow", "SOL", f"Largest single buy in SOL in the last {w}s (trade stream)")(max_buy)


for _w in (30, 60, 300):
    _flow(_w)


@feature("trades_total", "flow", "count", "Total trades seen (trade stream)")
def _ttot(st, now, m):
    return st.n_buys + st.n_sells if st.has_trade_stream else None


@feature("volume_total_sol", "flow", "SOL", "Total SOL volume seen (trade stream)")
def _vtot(st, now, m):
    return st.vol_buy + st.vol_sell if st.has_trade_stream else None


@feature("unique_buyers_total", "flow", "count", "Distinct buyers seen (trade stream)")
def _ubt(st, now, m):
    return len(st.buyers) if st.has_trade_stream else None


@feature("secs_since_trade", "flow", "s", "Seconds since last observed trade (trade stream)")
def _sst(st, now, m):
    return now - st.last_trade_ts if st.last_trade_ts else None


# ── holders (trade stream balances) ────────────────────────────────────────
def _top_pct(st, n: int):
    if not st.has_trade_stream or not st.balances:
        return None
    bals = sorted(st.balances.values(), reverse=True)[:n]
    return sum(bals) / curve.TOTAL_SUPPLY * 100


@feature("holders_est", "holders", "count", "Wallets with a positive balance (trade stream)")
def _hold(st, now, m):
    return sum(1 for b in st.balances.values() if b > 0) if st.has_trade_stream else None


@feature("top1_pct", "holders", "%", "Largest holder's share of supply, excluding the curve (trade stream)")
def _top1(st, now, m):
    return _top_pct(st, 1)


@feature("top10_pct", "holders", "%", "Top-10 holders' share of supply, excluding the curve (trade stream)")
def _top10(st, now, m):
    return _top_pct(st, 10)


@feature("dev_hold_pct", "holders", "%", "Creator's current share of supply")
def _devh(st, now, m):
    if st.has_trade_stream or st.dev_peak_balance:
        return st.dev_balance / curve.TOTAL_SUPPLY * 100
    return _f(st.gmgn.get("dev_hold_pct"))


@feature("dev_sold", "holders", "bool", "Creator has sold any tokens")
def _devs(st, now, m):
    if st.has_trade_stream or st.creator:
        return st.dev_sold
    return None


@feature("dev_sold_pct", "holders", "%", "Share of the creator's peak balance already sold")
def _devsp(st, now, m):
    if st.dev_peak_balance <= 0:
        return None
    return (1 - st.dev_balance / st.dev_peak_balance) * 100


@feature("sniper_hold_pct", "holders", "%", f"Supply share held by wallets whose first buy was within {5}s of launch (bundle/sniper proxy)")
def _snipe(st, now, m):
    if not st.has_trade_stream or st.created_ts is None:
        return None
    early = {w for w, t in st.first_buy_ts.items() if t - st.created_ts <= 5 and w != st.creator}
    return sum(st.balances.get(w, 0.0) for w in early) / curve.TOTAL_SUPPLY * 100


# ── tracked wallets (KOL / smart money) ────────────────────────────────────
@feature("kol_buys", "wallets", "count", "Distinct KOL/smart/tracked wallets that bought this token")
def _kb(st, now, m):
    return len({w for t, w, _ in st.wallet_buys if t <= now})


@feature("kol_buys_600s", "wallets", "count", "Distinct KOL/smart/tracked wallets that bought in the last 10 min")
def _kb600(st, now, m):
    return len({w for t, w, _ in st.wallet_buys if now - 600 <= t <= now})


@feature("kol_sells", "wallets", "count", "Distinct KOL/smart/tracked wallets that sold this token")
def _ks(st, now, m):
    return len({w for t, w, _ in st.wallet_sells if t <= now})


@feature("kol_label_kol", "wallets", "count", "Distinct GMGN-tagged KOL wallets that bought")
def _kk(st, now, m):
    return len({w for t, w, lbl in st.wallet_buys if lbl == "kol" and t <= now})


@feature("kol_label_smart", "wallets", "count", "Distinct GMGN smart-money wallets that bought")
def _km(st, now, m):
    return len({w for t, w, lbl in st.wallet_buys if lbl == "smartmoney" and t <= now})


@feature("secs_since_first_kol", "wallets", "s", "Seconds since the first tracked-wallet buy")
def _sfk(st, now, m):
    ts = [t for t, _, _ in st.wallet_buys if t <= now]
    return now - min(ts) if ts else None


# ── GMGN snapshot (free polling) ───────────────────────────────────────────
def _gmgn(key: str, desc: str, unit: str = ""):
    feature(f"gmgn_{key}", "gmgn", unit, f"GMGN: {desc}")(lambda st, now, m: _f(st.gmgn.get(key)))


_gmgn("holders", "holder count", "count")
_gmgn("top10_pct", "top-10 holder share", "%")
_gmgn("dev_hold_pct", "dev/team holding share", "%")
_gmgn("rug_ratio", "rug ratio (0-1, higher = riskier)", "ratio")
_gmgn("bundler_rate", "bundler trader share (0-1)", "ratio")
_gmgn("insider_rate", "suspected insider share (0-1)", "ratio")
_gmgn("sniper_count", "sniper wallet count", "count")
_gmgn("smart_degen_count", "smart-money wallets holding", "count")
_gmgn("kol_count", "KOL (renowned) wallets holding", "count")
_gmgn("volume_1h_usd", "1h volume in USD", "USD")
_gmgn("liquidity_usd", "liquidity in USD", "USD")
_gmgn("swaps_1h", "swaps in the last hour", "count")


@feature("socials_count", "gmgn", "count", "Number of social links (twitter/telegram/website) present")
def _soc(st, now, m):
    src = {**st.dex, **st.gmgn}
    if not src:
        return None
    return sum(1 for k in ("twitter", "telegram", "website") if src.get(k))


@feature("has_twitter", "gmgn", "bool", "Has a Twitter/X link")
def _tw(st, now, m):
    src = {**st.dex, **st.gmgn}
    return bool(src.get("twitter")) if src else None


# ── DexScreener snapshot (free polling) ────────────────────────────────────
def _dex(key: str, desc: str, unit: str = ""):
    feature(f"dex_{key}", "dex", unit, f"DexScreener: {desc}")(lambda st, now, m: _f(st.dex.get(key)))


_dex("liquidity_usd", "pool liquidity in USD", "USD")
_dex("buys_m5", "buy txns in the last 5 min", "count")
_dex("sells_m5", "sell txns in the last 5 min", "count")
_dex("volume_m5_usd", "volume in the last 5 min (USD)", "USD")
_dex("volume_h1_usd", "volume in the last hour (USD)", "USD")
_dex("price_chg_m5", "price change in the last 5 min", "%")
_dex("price_chg_h1", "price change in the last hour", "%")


@feature("dex_buy_ratio_m5", "dex", "ratio", "DexScreener buys/sells txn ratio over 5 min")
def _dbr(st, now, m):
    b, s = _f(st.dex.get("buys_m5")), _f(st.dex.get("sells_m5"))
    if b is None or s is None:
        return None
    return b / s if s > 0 else b


# ── discrete signals ───────────────────────────────────────────────────────
@feature("dex_paid", "signals", "bool", "Token has a DexScreener paid profile or boost")
def _dpaid(st, now, m):
    return any(k in st.signals and st.signals[k] <= now for k in ("dex_boost", "dex_profile"))


@feature("trench_new", "signals", "bool", "Listed in GMGN trenches 'New'")
def _tn(st, now, m):
    return "gmgn_new" in st.signals


@feature("trench_almost", "signals", "bool", "Listed in GMGN trenches 'Almost' (near graduation)")
def _ta(st, now, m):
    return "gmgn_almost" in st.signals


@feature("trench_migrated", "signals", "bool", "Listed in GMGN trenches 'Migrated'")
def _tm(st, now, m):
    return "gmgn_migrated" in st.signals


@feature("external_signals", "signals", "count", "Number of distinct external signals seen (trenches, boosts, Axiom/j7/fomo adapters)")
def _xs(st, now, m):
    return sum(1 for t in st.signals.values() if t <= now)


# ── safety ─────────────────────────────────────────────────────────────────
@feature("hard_rug", "safety", "bool", "Deterministic rug veto (honeypot, extreme concentration, high rug ratio, dev hoard)")
def _hr(st, now, m):
    return bool(rug.hard_rug_reasons(st))


# ── market regime ──────────────────────────────────────────────────────────
@feature("launches_per_min", "regime", "count/min", "pump.fun launches per minute over the last 10 min (market heat)")
def _lpm(st, now, m):
    return len(m.launch_ts) / 10.0 if m.launch_ts else None


@feature("sol_usd", "regime", "USD", "SOL price in USD")
def _solusd(st, now, m):
    return m.sol_usd


# ── evaluation helpers ─────────────────────────────────────────────────────
class FeatureView:
    """Lazy, memoized feature access for one token at one instant."""

    __slots__ = ("st", "now", "m", "_cache")

    def __init__(self, st, now: float, market):
        self.st, self.now, self.m = st, now, market
        self._cache: dict[str, Any] = {}

    def get(self, name: str):
        if name in self._cache:
            return self._cache[name]
        f = FEATURES.get(name)
        if f is None:
            raise KeyError(f"unknown feature {name!r}")
        try:
            v = f.fn(self.st, self.now, self.m)
        except (ZeroDivisionError, ValueError, TypeError):
            v = None
        self._cache[name] = v
        return v

    def snapshot(self) -> dict[str, Any]:
        out = {}
        for name in FEATURES:
            v = self.get(name)
            if v is not None:
                out[name] = round(v, 6) if isinstance(v, float) else v
        return out


def dictionary_text() -> str:
    """Deterministic feature dictionary for the (cached) AI system prompt."""
    lines = []
    group = None
    for f in sorted(FEATURES.values(), key=lambda f: (f.group, f.name)):
        if f.group != group:
            group = f.group
            lines.append(f"\n[{group}]")
        unit = f" ({f.unit})" if f.unit else ""
        lines.append(f"- {f.name}{unit}: {f.description}")
    return "\n".join(lines).strip()


# platform-specific features (solmeme/features/ext/*.py) register themselves on import
from solmeme.features import ext as _ext  # noqa: E402,F401
