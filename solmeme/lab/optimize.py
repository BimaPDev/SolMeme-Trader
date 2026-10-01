"""Parameter search with optuna over replay, walk-forward.

Trials are evaluated in batches: ask K trials, replay each fold ONCE with K
runners side by side (the engine isolates them), tell K results. The
objective is the median fold expectancy (% per trade), shrunk toward zero for
small samples and penalized when PnL depends on the top-3 trades.
Optimizer output is a *proposal* — it still has to pass the holdout and
shadow gates like an AI proposal.
"""

from __future__ import annotations

import math

import numpy as np
import optuna

from solmeme.config import Settings
from solmeme.lab.metrics import engine_metrics
from solmeme.sim.replay import run_replay
from solmeme.strategy.spec import StrategySpec


def _folds(events: list, k: int) -> list[list]:
    if k <= 1 or not events:
        return [events]
    t0, t1 = events[0].ts, events[-1].ts
    edges = [t0 + (t1 - t0) * i / k for i in range(k + 1)]
    out: list[list] = [[] for _ in range(k)]
    j = 0
    for ev in events:
        while j < k - 1 and ev.ts >= edges[j + 1]:
            j += 1
        out[j].append(ev)
    return [f for f in out if f]


def score(m, min_trades: int) -> float:
    if m.n_trades == 0:
        return -100.0
    shrink = math.sqrt(min(m.n_trades, min_trades) / min_trades)
    s = m.expectancy_pct * shrink
    if m.pnl_ex_top3_sol < 0:
        s -= 10
    return s


def optimize(settings: Settings, spec: StrategySpec, events: list, trials: int = 64, batch: int = 8,
             folds: int = 3, min_trades: int = 20, seed: int = 0) -> tuple[StrategySpec, list[dict]]:
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=seed))
    study.enqueue_trial({k: p.value for k, p in spec.params.items()})
    fold_events = _folds(events, folds)
    history: list[dict] = []
    done = 0
    while done < trials:
        n = min(batch, trials - done)
        ts = [study.ask() for _ in range(n)]
        cands = []
        for t in ts:
            vals = {}
            for k, p in spec.params.items():
                if p.type == "int":
                    vals[k] = float(t.suggest_int(k, int(p.lo), int(p.hi)))
                else:
                    vals[k] = t.suggest_float(k, p.lo, p.hi)
            cands.append(spec.with_params(vals))
        fold_scores: list[list[float]] = [[] for _ in cands]
        for fe in fold_events:
            # distinct ids keep identical candidates from colliding inside one engine
            runners = [(c.model_copy(update={"id": f"{c.id}#{i}"}), "candidate") for i, c in enumerate(cands)]
            eng = run_replay(settings, runners, fe, snapshot_features=False, record_near_misses=False)
            ms = engine_metrics(eng)
            for i, (rs, _) in enumerate(runners):
                fold_scores[i].append(score(ms[rs.key], max(3, min_trades // len(fold_events))))
        for t, c, fs in zip(ts, cands, fold_scores):
            val = float(np.median(fs))
            study.tell(t, val)
            history.append(dict(trial=t.number, score=val, params={k: p.value for k, p in c.params.items()}))
        done += n
    best = spec.with_params(study.best_params)
    return best, sorted(history, key=lambda h: -h["score"])
