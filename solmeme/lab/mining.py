"""Pattern mining over a strategy's journal: which entry features separate
winners from losers, which exits leave money on the table, which rules block
tokens that later ran (near-misses). This is the evidence the AI reviews."""

from __future__ import annotations

import json
from collections import defaultdict

import numpy as np


def _feat_matrix(trades: list[dict]) -> tuple[list[str], np.ndarray]:
    rows = []
    for t in trades:
        f = t.get("entry_features") or {}
        if isinstance(f, str):
            f = json.loads(f)
        rows.append(f)
    names = sorted({k for r in rows for k, v in r.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
                   | {k for r in rows for k, v in r.items() if isinstance(v, bool)})
    X = np.full((len(rows), len(names)), np.nan)
    for i, r in enumerate(rows):
        for j, n in enumerate(names):
            v = r.get(n)
            if isinstance(v, bool):
                X[i, j] = float(v)
            elif isinstance(v, (int, float)):
                X[i, j] = float(v)
    return names, X


def feature_lift(trades: list[dict], top: int = 15) -> list[dict]:
    """Winners vs losers per entry feature: medians and AUC (0.5 = no signal)."""
    from sklearn.metrics import roc_auc_score

    if len(trades) < 8:
        return []
    y = np.array([1 if float(t["pnl_sol"]) > 0 else 0 for t in trades])
    if y.min() == y.max():
        return []
    names, X = _feat_matrix(trades)
    out = []
    for j, n in enumerate(names):
        col = X[:, j]
        mask = ~np.isnan(col)
        if mask.sum() < 8 or len(set(y[mask])) < 2 or np.nanstd(col) == 0:
            continue
        auc = roc_auc_score(y[mask], col[mask])
        out.append(dict(feature=n, auc=round(float(auc), 3), coverage=round(float(mask.mean()), 2),
                        median_winners=round(float(np.nanmedian(col[mask & (y == 1)])), 4) if (mask & (y == 1)).any() else None,
                        median_losers=round(float(np.nanmedian(col[mask & (y == 0)])), 4) if (mask & (y == 0)).any() else None))
    out.sort(key=lambda d: -abs(d["auc"] - 0.5))
    return out[:top]


def tree_rules(trades: list[dict]) -> str:
    """Shallow decision tree (depth<=3) predicting a winning trade, as readable rules."""
    from sklearn.tree import DecisionTreeClassifier, export_text

    if len(trades) < 30:
        return "(too few trades for a tree)"
    y = np.array([1 if float(t["pnl_sol"]) > 0 else 0 for t in trades])
    if y.min() == y.max():
        return "(all trades have the same outcome)"
    names, X = _feat_matrix(trades)
    keep = [j for j in range(len(names)) if (~np.isnan(X[:, j])).mean() >= 0.6]
    if not keep:
        return "(no well-covered features)"
    X = X[:, keep]
    names = [names[j] for j in keep]
    med = np.nanmedian(X, axis=0)
    X = np.where(np.isnan(X), med, X)
    clf = DecisionTreeClassifier(max_depth=3, min_samples_leaf=max(5, len(trades) // 12), random_state=0)
    clf.fit(X, y)
    return export_text(clf, feature_names=names, decimals=3, show_weights=True)


def exit_summary(trades: list[dict]) -> list[dict]:
    g: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        g[t.get("exit_reason") or "?"].append(t)
    out = []
    for reason, ts in sorted(g.items(), key=lambda kv: -len(kv[1])):
        pae = [t["peak_after_exit_x"] for t in ts if t.get("peak_after_exit_x")]
        out.append(dict(
            exit_reason=reason, n=len(ts), avg_pnl_pct=round(float(np.mean([t["pnl_pct"] for t in ts])), 1),
            avg_mfe_x=round(float(np.mean([t["mfe_x"] or 1 for t in ts])), 2),
            avg_peak_after_exit_x=round(float(np.mean(pae)), 2) if pae else None,
            share_ran_2x_after_exit=round(sum(1 for p in pae if p >= 2) / len(pae), 2) if pae else None,
        ))
    return out


def near_miss_summary(near_misses: list[dict]) -> list[dict]:
    """Per blocking rule: how many tokens it blocked and how they did afterwards."""
    g: dict[str, list[dict]] = defaultdict(list)
    for nm in near_misses:
        if nm.get("max_x_after") is not None:
            g[nm["failed_rule"]].append(nm)
    out = []
    for rule, rows in g.items():
        mx = [r["max_x_after"] for r in rows]
        mn = [r["min_x_after"] for r in rows if r.get("min_x_after") is not None]
        out.append(dict(rule=rule, blocked=len(rows),
                        share_ran_2x=round(sum(1 for x in mx if x >= 2) / len(mx), 2),
                        share_ran_3x=round(sum(1 for x in mx if x >= 3) / len(mx), 2),
                        share_dumped_50=round(sum(1 for x in mn if x <= 0.5) / len(mn), 2) if mn else None,
                        median_max_x=round(float(np.median(mx)), 2)))
    out.sort(key=lambda d: -d["blocked"])
    return out
