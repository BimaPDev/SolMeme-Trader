"""Shared contract for the launch sniper.

The analyzer, the online learner, and the session all import this module.
Do not invent parallel feature lists or bin edges elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field

STRATEGY_ID = "launch_sniper"

# Numeric features the learner is allowed to weight. Missing values are omitted
# from the vector (the model treats them as neutral, z = 0).
LEARN_FEATURES: tuple[str, ...] = (
    "unique_buyers_30s",
    "buy_sell_ratio_30s",
    "net_flow_30s",
    "buy_vol_30s",
    "max_buy_30s",
    "sells_30s",
    "top10_pct",
    "sniper_hold_pct",
    "dev_hold_pct",
    "bonding_pct",
    "price_chg_30s_pct",
    "x_from_first_price",
    "holders_est",
    "launches_per_min",
)

# Lower edges. Bin i is [edges[i], edges[i+1]) and the last bin is [edges[-1], +inf).
BIN_EDGES: dict[str, tuple[float, ...]] = {
    "unique_buyers_30s": (0, 3, 8, 15, 40),
    "buy_sell_ratio_30s": (0, 1, 1.5, 3, 8),
    "net_flow_30s": (-1e9, 0, 1, 4, 12),
    "buy_vol_30s": (0, 0.5, 2, 6, 20),
    "max_buy_30s": (0, 0.5, 1.5, 4, 10),
    "sells_30s": (0, 1, 3, 8, 20),
    "top10_pct": (0, 20, 35, 50, 70),
    "sniper_hold_pct": (0, 5, 15, 30, 50),
    "dev_hold_pct": (0, 2, 8, 15, 30),
    "bonding_pct": (0, 10, 30, 55, 80),
    "price_chg_30s_pct": (-100, 0, 15, 50, 150),
    "x_from_first_price": (0, 1, 1.3, 2, 4),
    "holders_est": (0, 5, 12, 25, 60),
    "launches_per_min": (0, 3, 8, 15, 30),
}

# Domain prior, applied to z-scored features. Positive = more of this predicts a runner.
PRIOR_WEIGHTS: dict[str, float] = {
    "unique_buyers_30s": 0.60,
    "buy_sell_ratio_30s": 0.35,
    "net_flow_30s": 0.25,
    "buy_vol_30s": 0.15,
    "max_buy_30s": -0.25,
    "sells_30s": -0.20,
    "top10_pct": -0.35,
    "sniper_hold_pct": -0.45,
    "dev_hold_pct": -0.20,
    "bonding_pct": -0.15,
    "price_chg_30s_pct": 0.20,
    "x_from_first_price": -0.10,
    "holders_est": 0.15,
    "launches_per_min": -0.10,
}

# (mean, std) used before a feature has enough observations, and blended after.
PRIOR_MOMENTS: dict[str, tuple[float, float]] = {
    "unique_buyers_30s": (6.0, 8.0),
    "buy_sell_ratio_30s": (1.5, 2.0),
    "net_flow_30s": (1.0, 4.0),
    "buy_vol_30s": (2.0, 5.0),
    "max_buy_30s": (1.0, 2.0),
    "sells_30s": (2.0, 4.0),
    "top10_pct": (35.0, 20.0),
    "sniper_hold_pct": (15.0, 15.0),
    "dev_hold_pct": (8.0, 10.0),
    "bonding_pct": (20.0, 25.0),
    "price_chg_30s_pct": (10.0, 40.0),
    "x_from_first_price": (1.3, 0.8),
    "holders_est": (12.0, 15.0),
    "launches_per_min": (8.0, 6.0),
}

PRIOR_BIAS = -0.40
PRIOR_STRENGTH = 8.0  # pseudo-observations blended into the running mean/var


def bin_label(feature: str, value: float) -> str | None:
    edges = BIN_EDGES.get(feature)
    if edges is None:
        return None
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        if lo <= value < hi:
            return f"[{_fmt(lo)},{_fmt(hi)})"
    if value >= edges[-1]:
        return f"[{_fmt(edges[-1])},inf)"
    return None


def _fmt(x: float) -> str:
    if abs(x) >= 1e8:
        return "-inf" if x < 0 else "inf"
    if x == int(x):
        return str(int(x))
    return f"{x:g}"


@dataclass
class LaunchCard:
    mint: str
    ts: float
    symbol: str
    price: float | None
    vetoes: list[str]
    parts: dict[str, float]
    vector: dict[str, float]
    note: str
    snapshot: dict = field(default_factory=dict)
