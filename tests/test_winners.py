"""Learning from live winners: keeper rules, inherited exits, credit, stress tests."""

import json
import random
import time

import numpy as np

from solmeme.lab.evolve import Breeder, Credit
from solmeme.lab.winners import bootstrap_profit, keeper_rules, stress


def test_keeper_rules_find_what_winning_trades_shared():
    rng = np.random.default_rng(1)
    trades = []
    for _ in range(60):
        h = float(rng.uniform(10, 200))
        trades.append({"pnl_sol": 0.05 if h >= 80 else -0.02, "pnl_pct": 0,
                       "entry_features": {"gmgn_holders": h, "noise": float(rng.normal())}})
    ks = keeper_rules(trades)
    assert ks[0]["feature"] == "gmgn_holders" and ks[0]["op"] == ">=" and 60 <= ks[0]["value"] <= 85
    assert ks[0]["keeps_profit"] >= 0.8 and ks[0]["saves_sol"] > 0


def test_children_inherit_winner_rules_and_exit_ladders(specs):
    w = dict(key="holder_dip@x", strategy_id="near_graduation", equity=5.0,
             keepers=[dict(feature="gmgn_holders", op=">=", value=50.0, saves_sol=1.0, keeps_profit=0.9, drops=10)],
             ladder=[[2.0, 50.0], [5.0, 100.0]])
    br = Breeder(random.Random(0), Credit([]), {}, {}, winners=[w])
    sig, ch = br._winner_rule(specs["near_graduation"])
    assert sig == "winner_rule:gmgn_holders:>=" and ch.section == "all" and ch.value == 50.0
    assert br._winner_rule(specs["dex_curve_momentum"]) is None              # keeper rules stay in their family
    sig, ch = br._adopt_exits(specs["dex_curve_momentum"])                     # exits travel across families
    assert sig == "adopt_exits:near_graduation" and ch.take_profit == [[2.0, 50.0], [5.0, 100.0]]


def test_thriving_agents_credit_their_mutations():
    rows = [dict(strategy_id="s", ops=json.dumps(["param:sl:-"]), event="thriving", fitness=None, parent_fitness=None)]
    assert Credit(rows).weight("s", "param:sl:-") > 2


def test_bootstrap_profit_separates_consistent_from_lucky():
    assert bootstrap_profit([20, 15, -5, 30, 10, 25, -8, 12])[0] > 0.95
    assert bootstrap_profit([-20, -15, -5, 300, -10, -25, -8, -12])[0] < 0.9


def test_stress_scores_every_scenario(settings, specs, events):
    s = specs["dex_curve_momentum"]
    a, b = events[0].ts, events[-1].ts
    res = stress(settings, [s], lambda x, y: [e for e in events if x <= e.ts < y], (a, b), {s.key: []})[s.key]
    assert len(res["scenarios"]) == 7 and 0.0 <= res["robustness"] <= 1.0
    assert "2x slower fills" in res["scenarios"] and "without its 3 best trades" in res["scenarios"]


def test_market_wide_features_never_become_rules():
    rng = np.random.default_rng(2)
    trades = [{"pnl_sol": 0.05 if i < 30 else -0.02, "pnl_pct": 0,
               "entry_features": {"sol_usd": 100.0 + i, "launches_per_min": float(i), "noise": float(rng.normal())}}
              for i in range(60)]
    assert all(k["feature"] not in ("sol_usd", "launches_per_min") for k in keeper_rules(trades))
