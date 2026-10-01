"""Builds the per-review evidence pack (the volatile user turn): compact tables
the reviewer reasons over. Only evidence-window data goes in — never holdout."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from solmeme.lab import mining
from solmeme.lab.metrics import Metrics
from solmeme.strategy.spec import StrategySpec

MAX_TRADES = 60


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def _csv(rows: list[dict], cols: list[str]) -> str:
    out = [",".join(cols)]
    for r in rows:
        out.append(",".join(_fmt(r.get(c)) for c in cols))
    return "\n".join(out)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def build(spec: StrategySpec, metrics: Metrics, trades: list[dict], near_misses: list[dict], lessons: list[dict],
          universe_lift: list[dict], window: tuple[float, float]) -> str:
    rule_feats = [c.f for _, c in spec.entry.rules()]
    lift = mining.feature_lift(trades)
    lift_feats = [d["feature"] for d in lift[:10]]
    feat_cols = list(dict.fromkeys([*rule_feats, *lift_feats]))[:20]

    # most informative trades: biggest losers, biggest winners, and "sold too early" ones
    by_pnl = sorted(trades, key=lambda t: t["pnl_sol"])
    early = sorted(trades, key=lambda t: -(t.get("peak_after_exit_x") or 0))[:10]
    chosen = {t["trade_id"]: t for t in [*by_pnl[:25], *by_pnl[-15:], *early]}
    rows = []
    for t in sorted(chosen.values(), key=lambda t: t["entry_ts"])[:MAX_TRADES]:
        f = t.get("entry_features") or {}
        if isinstance(f, str):
            f = json.loads(f)
        rows.append({
            "trade_id": t["trade_id"].split(":", 1)[-1], "pnl_pct": t["pnl_pct"], "pnl_sol": t["pnl_sol"],
            "exit": t["exit_reason"], "hold_s": t["hold_s"], "mfe_x": t.get("mfe_x"), "mae_x": t.get("mae_x"),
            "after_exit_peak_x": t.get("peak_after_exit_x"), "fails": t.get("failed_attempts"),
            **{c: f.get(c) for c in feat_cols},
        })

    params = {k: {"value": p.value, "lo": p.lo, "hi": p.hi, "type": p.type} for k, p in spec.params.items()}
    lessons_txt = "\n".join(
        f"- [{l['status']}] {l.get('hypothesis', '')[:200]} | changes={l.get('changes_json', '')[:300]} | "
        f"result={l.get('reason', '')[:200]}" for l in lessons) or "(none yet)"

    parts = [
        f"# Strategy {spec.id} @ {spec.version}",
        f"Evidence window: {_iso(window[0])} → {_iso(window[1])} UTC (replayed with the live engine).",
        "## Spec (YAML)\n```yaml\n" + spec.to_yaml() + "```",
        "## Tunable params (bounds are hard limits)\n" + json.dumps(params),
        "## Metrics\n" + json.dumps({k: (round(v, 4) if isinstance(v, float) else v)
                                     for k, v in metrics.as_dict().items()}),
        "## Exit breakdown\n" + _csv(mining.exit_summary(trades),
                                     ["exit_reason", "n", "avg_pnl_pct", "avg_mfe_x", "avg_peak_after_exit_x",
                                      "share_ran_2x_after_exit"]),
        "## Winners vs losers by entry feature (AUC>0.5: higher value → more likely a winner)\n"
        + _csv(lift, ["feature", "auc", "coverage", "median_winners", "median_losers"]),
        "## Shallow decision tree (class 1 = winning trade)\n```\n" + mining.tree_rules(trades) + "\n```",
        "## Near-misses: tokens blocked by exactly ONE rule, and what they did in the next 30 min\n"
        + _csv(mining.near_miss_summary(near_misses),
               ["rule", "blocked", "share_ran_2x", "share_ran_3x", "share_dumped_50", "median_max_x"]),
        "## Universe scan: what predicts 'hits 2x before -50%' across ALL tokens (not only traded ones)\n"
        + (_csv(universe_lift, ["feature", "auc", "coverage", "base_win_rate", "best_rule", "rule_win_rate", "rule_n"])
           if universe_lift else "(not enough data)"),
        f"## Trades (selected {len(rows)} of {len(trades)}: worst, best, biggest post-exit runs)\n"
        + _csv(rows, list(rows[0].keys()) if rows else ["trade_id"]),
        "## Lessons from earlier proposals for this strategy\n" + lessons_txt,
    ]
    return "\n\n".join(parts)
