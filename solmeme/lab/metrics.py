"""Per-strategy-version performance. Meme PnL is dominated by outliers, so we
report robust variants (median, PnL ex-top-3) next to the usual numbers."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass

import numpy as np


@dataclass
class Metrics:
    key: str
    n_trades: int
    win_rate: float
    total_pnl_sol: float
    expectancy_sol: float
    expectancy_pct: float
    median_pct: float
    profit_factor: float
    max_dd_sol: float
    pnl_ex_top3_sol: float
    rug_hit_rate: float
    avg_hold_s: float
    fees_sol: float
    failed_buys: int = 0
    failed_buy_cost_sol: float = 0.0
    early_exit_rate: float = 0.0     # exited, then token ran >= 2x from our exit price

    def as_dict(self) -> dict:
        return asdict(self)


def _max_drawdown(pnls: list[float]) -> float:
    eq = np.cumsum([0.0, *pnls])
    peak = np.maximum.accumulate(eq)
    return float(np.max(peak - eq)) if len(eq) else 0.0


def compute(trades: list[dict], key: str = "", failed_buys: int = 0, failed_buy_cost: float = 0.0) -> Metrics:
    trades = sorted(trades, key=lambda t: t.get("exit_ts") or 0)
    pnl = [float(t["pnl_sol"]) for t in trades]
    pct = [float(t["pnl_pct"]) for t in trades]
    n = len(trades)
    if n == 0:
        return Metrics(key, 0, 0, -failed_buy_cost, 0, 0, 0, 0, 0, -failed_buy_cost, 0, 0, 0,
                       failed_buys, failed_buy_cost, 0)
    wins = [p for p in pnl if p > 0]
    losses = [-p for p in pnl if p < 0]
    pf = (sum(wins) / sum(losses)) if losses else (float("inf") if wins else 0.0)
    total = sum(pnl) - failed_buy_cost
    ex_top3 = sum(sorted(pnl)[:-3]) if n > 3 else min(0.0, sum(pnl))
    rugs = sum(1 for t in trades if (t.get("exit_reason") == "rug") or float(t["pnl_pct"]) <= -80)
    early = sum(1 for t in trades if (t.get("peak_after_exit_x") or 0) >= 2.0)
    return Metrics(
        key=key, n_trades=n, win_rate=len(wins) / n, total_pnl_sol=total,
        expectancy_sol=total / n, expectancy_pct=float(np.mean(pct)), median_pct=float(np.median(pct)),
        profit_factor=pf, max_dd_sol=_max_drawdown(pnl), pnl_ex_top3_sol=ex_top3 - failed_buy_cost,
        rug_hit_rate=rugs / n, avg_hold_s=float(np.mean([t.get("hold_s") or 0 for t in trades])),
        fees_sol=sum(float(t.get("fees_sol") or 0) for t in trades) + failed_buy_cost,
        failed_buys=failed_buys, failed_buy_cost_sol=failed_buy_cost, early_exit_rate=early / n,
    )


def by_version(trades: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        out[f"{t['strategy_id']}@{t['version']}"].append(t)
    return out


def engine_metrics(engine) -> dict[str, Metrics]:
    grouped = by_version(engine.journal.trades)
    return {
        r.key: compute(grouped.get(r.key, []), r.key, r.failed_buys, r.failed_buy_cost)
        for r in engine.runners
    }


def bootstrap_diff_ci(a: list[float], b: list[float], iters: int = 2000, conf: float = 0.9,
                      seed: int = 0) -> tuple[float, float, float]:
    """CI for mean(a) - mean(b) by independent resampling. Returns (lo, point, hi)."""
    if not a or not b:
        return float("-inf"), 0.0, float("inf")
    rng = np.random.default_rng(seed)
    A, B = np.asarray(a, float), np.asarray(b, float)
    da = rng.choice(A, (iters, len(A))).mean(axis=1)
    db = rng.choice(B, (iters, len(B))).mean(axis=1)
    d = da - db
    lo, hi = np.quantile(d, [(1 - conf) / 2, 1 - (1 - conf) / 2])
    return float(lo), float(A.mean() - B.mean()), float(hi)
