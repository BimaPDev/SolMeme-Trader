"""Universe scan: strategy-independent pattern discovery.

Replays the market (no strategies), snapshots every token's features at fixed
ages, then labels each snapshot with what happened next (did it 2x before
dropping 50%? max multiple within 30 min?). Feature lift over thousands of
token-moments answers "what predicts a runner?" far better than a strategy's
own few dozen trades. Labels live only here — never as live features.
"""

from __future__ import annotations

import heapq
from collections import defaultdict

import numpy as np

from solmeme.features.registry import FeatureView
from solmeme.features.token_state import MarketState

CHECKPOINTS_S = (30, 90, 300, 900)
HORIZON_S = 1800


def scan(events, excluded: set[str] | None = None, checkpoints=CHECKPOINTS_S, horizon_s: float = HORIZON_S) -> list[dict]:
    m = MarketState(excluded=excluded)
    due: list = []
    scheduled: set[str] = set()
    open_rows: dict[str, list[dict]] = defaultdict(list)
    rows: list[dict] = []
    for ev in events:
        while due and due[0][0] <= ev.ts:
            t, cp, mint = heapq.heappop(due)
            st = m.get(mint)
            if st is None or st.price_sol is None:
                continue
            snap = FeatureView(st, t, m).snapshot()
            row = {"mint": mint, "checkpoint_s": cp, "ts": t, "p0": st.price_sol, "max_p": st.price_sol,
                   "min_p": st.price_sol, "hit": None, "end": t + horizon_s, "features": snap}
            open_rows[mint].append(row)
            rows.append(row)
        st = m.apply(ev)
        if st is None:
            continue
        if st.mint not in scheduled and (ev.kind == "launch" or st.created_ts is not None):
            scheduled.add(st.mint)
            base = st.age_ref_ts
            for cp in checkpoints:
                if base + cp > ev.ts:
                    heapq.heappush(due, (base + cp, cp, st.mint))
        if st.price_sol is not None and st.mint in open_rows:
            alive = []
            for r in open_rows[st.mint]:
                if ev.ts > r["end"]:
                    continue
                p = st.price_sol
                r["max_p"] = max(r["max_p"], p)
                r["min_p"] = min(r["min_p"], p)
                if r["hit"] is None:
                    if p >= 2 * r["p0"]:
                        r["hit"] = True
                    elif p <= 0.5 * r["p0"]:
                        r["hit"] = False
                alive.append(r)
            open_rows[st.mint] = alive
    for r in rows:
        r["max_x"] = r["max_p"] / r["p0"]
        r["min_x"] = r["min_p"] / r["p0"]
        r["win"] = bool(r["hit"])
    return rows


def lift_table(rows: list[dict], checkpoint_s: int | None = None, top: int = 20, min_rows: int = 40) -> list[dict]:
    """Per feature: AUC for 'hits 2x before -50%' and the best single threshold."""
    from sklearn.metrics import roc_auc_score

    rows = [r for r in rows if checkpoint_s is None or r["checkpoint_s"] == checkpoint_s]
    if len(rows) < min_rows:
        return []
    y = np.array([1 if r["win"] else 0 for r in rows])
    if y.min() == y.max():
        return []
    base = y.mean()
    names = sorted({k for r in rows for k, v in r["features"].items() if isinstance(v, (int, float))})
    out = []
    for n in names:
        col = np.array([float(r["features"].get(n)) if isinstance(r["features"].get(n), (int, float)) else np.nan
                        for r in rows])
        mask = ~np.isnan(col)
        if mask.sum() < min_rows or len(set(y[mask])) < 2 or np.nanstd(col) == 0:
            continue
        auc = float(roc_auc_score(y[mask], col[mask]))
        direction = ">=" if auc >= 0.5 else "<"
        best = None
        for q in (0.5, 0.6, 0.7, 0.8, 0.9) if auc >= 0.5 else (0.5, 0.4, 0.3, 0.2, 0.1):
            thr = float(np.quantile(col[mask], q))
            sel = (col >= thr) if direction == ">=" else (col < thr)
            sel &= mask
            if sel.sum() < 15:
                continue
            rate = float(y[sel].mean())
            if best is None or rate > best[1]:
                best = (thr, rate, int(sel.sum()))
        out.append(dict(feature=n, auc=round(auc, 3), coverage=round(float(mask.mean()), 2),
                        base_win_rate=round(float(base), 3),
                        best_rule=f"{n} {direction} {best[0]:.4g}" if best else None,
                        rule_win_rate=round(best[1], 3) if best else None, rule_n=best[2] if best else None))
    out.sort(key=lambda d: -abs(d["auc"] - 0.5))
    return out[:top]
