"""Deterministic triage: decide — for free — whether a strategy review is worth an AI call.

Runs the same statistics the AI would read (exit breakdown, winner/loser feature AUCs,
near-misses, fees, rug hits) and turns them into plain-language flags. No flags → no API
call. Flags found → they are put at the top of the evidence pack so Claude starts from
them instead of re-deriving them (shorter, cheaper reviews).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from solmeme.config import TriageCfg
from solmeme.lab import mining
from solmeme.lab.metrics import Metrics


@dataclass
class Triage:
    call_ai: bool
    reason: str
    flags: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)     # value-free flag identities

    @property
    def signature(self) -> str:
        return hashlib.sha256("|".join(sorted(self.kinds)).encode()).hexdigest()[:16]


def triage(m: Metrics, trades: list[dict], near_misses: list[dict], cfg: TriageCfg) -> Triage:
    if m.n_trades < cfg.min_trades:
        return Triage(False, f"only {m.n_trades} trades (< {cfg.min_trades}) — too noisy to review")

    flags: list[str] = []
    kinds: list[str] = []
    for d in mining.feature_lift(trades, top=5):
        if abs(d["auc"] - 0.5) >= cfg.min_feature_auc_edge and d["coverage"] >= 0.6:
            better = "higher" if d["auc"] > 0.5 else "lower"
            flags.append(f"entry feature {d['feature']} separates winners from losers (AUC {d['auc']}; "
                         f"winners have {better} values: {d['median_winners']} vs {d['median_losers']})")
            kinds.append(f"auc:{d['feature']}")
    for d in mining.near_miss_summary(near_misses):
        if d["blocked"] >= 10 and d["share_ran_2x"] >= cfg.near_miss_2x_share:
            flags.append(f"rule {d['rule']} blocked {d['blocked']} tokens and {d['share_ran_2x']:.0%} of them "
                         f"ran 2x afterwards (filter may be too strict)")
            kinds.append(f"nearmiss:{d['rule']}")
    if m.early_exit_rate >= cfg.early_exit_share:
        flags.append(f"{m.early_exit_rate:.0%} of exits were followed by a ≥2x run (exits may be too early)")
        kinds.append("early_exit")
    if m.rug_hit_rate >= cfg.rug_hit_rate:
        flags.append(f"rug-hit rate {m.rug_hit_rate:.0%} (entries not filtering rugs)")
        kinds.append("rugs")
    gross_wins = sum(t["pnl_sol"] for t in trades if t["pnl_sol"] > 0)
    if gross_wins > 0 and m.fees_sol / gross_wins >= cfg.fee_drag_share:
        flags.append(f"fees are {m.fees_sol / gross_wins:.0%} of gross wins (fee drag)")
        kinds.append("fees")
    for d in mining.exit_summary(trades):
        if d["n"] >= 5 and d["avg_pnl_pct"] < -20 and d["n"] / m.n_trades >= 0.25:
            flags.append(f"{d['n']} trades ({d['n'] / m.n_trades:.0%}) exit via {d['exit_reason']} "
                         f"averaging {d['avg_pnl_pct']}%")
            kinds.append(f"exit:{d['exit_reason']}")

    healthy = m.expectancy_pct >= cfg.healthy_expectancy_pct and m.pnl_ex_top3_sol > 0
    money_flags = [k for k in kinds if not k.startswith("auc:")]
    if healthy and not money_flags:
        return Triage(False, f"healthy (expectancy {m.expectancy_pct:+.1f}%/trade); no money-losing pattern",
                      flags, kinds)
    if not flags:
        return Triage(False, "underperforming but no actionable statistical signal — gather more data")
    return Triage(True, f"{len(flags)} actionable flag(s)", flags, kinds)
