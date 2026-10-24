"""Learn from the strategies that are winning live, pass it on, and stress-test them.

Each swarm generation (no LLM):
  1. winners    the top live strategies by wallet growth (enough trades to mean something)
  2. explain    why each one wins: the changes in its family tree that led to it, the entry
                conditions its winning trades shared ("keeper" rules: cuts that would have kept
                most of the winning profit and dropped losers), and how its exits made the money
  3. pass on    winners get guaranteed children; their keeper rules and their exit ladder / bet
                size become mutations for new agents (exits also across strategy families); the
                changes in a winner's family tree earn credit, so the swarm tries them more
  4. stress     replay each winner over the recent window under worse conditions (slower fills,
                higher fees, more failed transactions, settings nudged, best trades removed) and
                resample its live trades; the share of scenarios it survives is its robustness,
                which promotion requires
Results go to <data>/winners.json for the website and the next generation.
"""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from solmeme.lab.mining import _feat_matrix

FILE = "winners.json"
TOP = 3
MIN_TRADES = 20


def load(data_dir: Path) -> dict:
    try:
        return json.loads((Path(data_dir) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(data_dir: Path, data: dict) -> None:
    path = Path(data_dir) / FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=float), encoding="utf-8")
    tmp.replace(path)


# ── explain ────────────────────────────────────────────────────────────────
def keeper_rules(trades: list[dict], top: int = 5, min_trades: int = 15) -> list[dict]:
    """Entry conditions the winning trades shared: "buy only when f op v" cuts that keep at least
    80% of the winning profit while removing losing SOL. The positive mirror of evolve's lessons."""
    if len(trades) < min_trades:
        return []
    pnl = np.array([float(t["pnl_sol"]) for t in trades])
    win_profit = pnl[pnl > 0].sum()
    if win_profit <= 0:
        return []
    names, X = _feat_matrix(trades, drop_market=True)
    out = []
    for j, name in enumerate(names):
        col = X[:, j]
        known = ~np.isnan(col)
        if known.sum() < min_trades or np.nanstd(col) == 0 or set(np.unique(col[known])) <= {0.0, 1.0}:
            continue
        best = None
        for q in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
            t = float(np.quantile(col[known], q))
            for op, keep in ((">=", known & (col >= t)), ("<=", known & (col <= t))):
                kept_win = pnl[keep & (pnl > 0)].sum()
                dropped = pnl[known & ~keep]
                if kept_win < 0.8 * win_profit or len(dropped) < 3:
                    continue
                gain = -float(dropped.sum())           # SOL the cut would have saved
                if gain > 0 and (best is None or gain > best["saves_sol"]):
                    best = dict(feature=name, op=op, value=round(t, 6), saves_sol=round(gain, 4),
                                keeps_profit=round(float(kept_win / win_profit), 3), drops=int(len(dropped)))
        if best:
            out.append(best)
    return sorted(out, key=lambda r: -r["saves_sol"])[:top]


def exit_mix(trades: list[dict]) -> list[dict]:
    g: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        g[t.get("exit_reason") or "?"].append(float(t["pnl_sol"] or 0))
    rows = [dict(exit=k, trades=len(v), pnl_sol=round(sum(v), 4), avg_sol=round(sum(v) / len(v), 4)) for k, v in g.items()]
    return sorted(rows, key=lambda r: -r["pnl_sol"])


def genes(store, sid: str, version: str, depth: int = 12) -> list[dict]:
    """The mutations along a strategy's family tree, newest first, with how each version did live."""
    out, cur, seen = [], version, set()
    while cur and cur not in seen and len(out) < depth:
        seen.add(cur)
        born = store.evo_rows(version=cur, event="born")
        row = store.get_version(sid, cur)
        if row is None:
            break
        since = born[0]["ts"] if born else row["created_ts"]
        tr = [t for t in store.trades(sid, cur, run_mode="live") if (t["entry_ts"] or 0) >= since]
        out.append(dict(version=cur, ops=json.loads(born[0]["ops"]) if born else [], trades=len(tr),
                        pnl_sol=round(sum(float(t["pnl_sol"] or 0) for t in tr), 4)))
        cur = row["parent_version"] or (born[0]["parent_version"] if born else None)
    return out


# ── stress ─────────────────────────────────────────────────────────────────
SCENARIOS = {
    "as recorded": {},
    "2x slower fills": {"latency": 2.0},
    "higher fees": {"fees": 2.0},
    "2x failed transactions": {"fail": 2.0},
    "settings nudged -10%": {"nudge": 0.9},
    "settings nudged +10%": {"nudge": 1.1},
}


def _stressed_settings(settings, cfg: dict):
    s = settings.model_copy(deep=True)
    f = s.fills
    if "latency" in cfg:
        f.latency_ms = (f.latency_ms[0] * cfg["latency"], f.latency_ms[1] * cfg["latency"])
    if "fees" in cfg:
        f.priority_fee_sol *= cfg["fees"]
        f.jito_tip_sol *= cfg["fees"]
        f.platform_fee_pct *= cfg["fees"]
        f.sandwich_prob = min(0.9, f.sandwich_prob * cfg["fees"])
    if "fail" in cfg:
        f.fail_rate = min(0.9, f.fail_rate * cfg["fail"])
    return s


def _nudged(spec, factor: float):
    vals = {}
    for k, p in spec.params.items():
        v = min(p.hi, max(p.lo, p.value * factor))
        vals[k] = float(round(v)) if p.type == "int" else v
    try:
        return spec.with_params(vals) if vals else spec
    except Exception:  # noqa: BLE001
        return spec


def bootstrap_profit(pcts: list[float], iters: int = 4000, seed: int = 0) -> tuple[float, float]:
    """(probability the mean trade is positive, 10th-percentile mean trade %) from the live trades."""
    if len(pcts) < 5:
        return 0.0, 0.0
    rng = np.random.default_rng(seed)
    a = np.asarray(pcts, float)
    means = rng.choice(a, (iters, len(a))).mean(axis=1)
    return float((means > 0).mean()), float(np.quantile(means, 0.1))


def stress(settings, specs: list, events_fn, window: tuple[float, float], live: dict[str, list[dict]]) -> dict[str, dict]:
    """Replay the specs under each scenario (one engine per scenario, all specs side by side)."""
    from solmeme.lab.metrics import compute, engine_metrics
    from solmeme.sim.replay import run_replay

    out: dict[str, dict] = {s.key: dict(scenarios={}) for s in specs}
    for name, cfg in SCENARIOS.items():
        st = _stressed_settings(settings, cfg)
        runners = [(_nudged(s, cfg["nudge"]) if "nudge" in cfg else s).model_copy(update={"id": f"{s.id}#stress"}) for s in specs]
        eng = run_replay(st, [(r, "candidate") for r in runners], events_fn(*window), snapshot_features=False,
                         record_near_misses=False)
        ms = engine_metrics(eng)
        by_key = defaultdict(list)
        for t in eng.journal.trades:
            by_key[f"{t['strategy_id']}@{t['version']}"].append(t)
        for s, r in zip(specs, runners):
            m = ms[r.key]
            out[s.key]["scenarios"][name] = dict(trades=m.n_trades, pnl_sol=round(m.total_pnl_sol, 4),
                                                 avg_pct=round(m.expectancy_pct, 1), passed=m.n_trades >= 5 and m.total_pnl_sol > 0)
            if name == "as recorded":
                tr = sorted(by_key.get(r.key, []), key=lambda t: -float(t["pnl_sol"]))
                rest = compute(tr[3:]) if len(tr) > 3 else None
                out[s.key]["scenarios"]["without its 3 best trades"] = dict(
                    trades=len(tr) - 3 if rest else 0, pnl_sol=round(rest.total_pnl_sol, 4) if rest else 0.0,
                    avg_pct=round(rest.expectancy_pct, 1) if rest else 0.0, passed=bool(rest and rest.total_pnl_sol > 0))
    for s in specs:
        sc = out[s.key]["scenarios"]
        p, low = bootstrap_profit([float(t["pnl_pct"] or 0) for t in live.get(s.key, [])], seed=len(s.key))
        out[s.key].update(robustness=round(sum(v["passed"] for v in sc.values()) / len(sc), 3),
                          live_p_profit=round(p, 3), live_p10_avg_pct=round(low, 1))
    return out


# ── the generation step ────────────────────────────────────────────────────
def update(settings, store, events_fn, window, start_sol: float, gen: int, log_fn=lambda _: None,
           now: float | None = None, do_stress: bool = True) -> dict:
    from solmeme.strategy.spec import spec_from_json

    from solmeme.sim.wallet import trusted_since

    now = now or time.time()
    trust_ts = trusted_since(settings)            # trades filled by the broken model don't make anyone a winner
    rows = store.query("SELECT * FROM strategy_versions WHERE status IN ('swarm','champion','trial','challenger')")
    cands = []
    for r in rows:
        born = store.evo_rows(version=r["version"], event="born")
        since = max(born[0]["ts"] if born else r["created_ts"], trust_ts)
        tr = [t for t in store.trades(r["strategy_id"], r["version"], run_mode="live") if (t["entry_ts"] or 0) >= since]
        if len(tr) < MIN_TRADES:
            continue
        pnl = sum(float(t["pnl_sol"] or 0) for t in tr)
        if pnl <= 0:
            continue
        cands.append((1 + pnl / start_sol, r, tr))
    cands.sort(key=lambda c: -c[0])
    prev = load(settings.data_dir)
    winners = []
    for equity, r, tr in cands[:TOP]:
        sid, v = r["strategy_id"], r["version"]
        spec = spec_from_json(r["spec_json"])
        ex = spec.exits
        winners.append(dict(
            key=f"{sid}@{v}", strategy_id=sid, version=v, status=r["status"], equity=round(equity, 4), trades=len(tr),
            win=round(sum(float(t["pnl_sol"]) > 0 for t in tr) / len(tr), 3),
            keepers=keeper_rules(tr), exits=exit_mix(tr), genes=genes(store, sid, v),
            hold_win_s=float(np.median([t["hold_s"] or 0 for t in tr if float(t["pnl_sol"]) > 0] or [0])),
            hold_loss_s=float(np.median([t["hold_s"] or 0 for t in tr if float(t["pnl_sol"]) <= 0] or [0])),
            ladder=[[float(spec.p(t.at_x)), float(spec.p(t.sell_pct))] for t in ex.take_profit],
            stop_loss_pct=spec.p(ex.stop_loss_pct), trailing_pct=spec.p(ex.trailing_pct),
            bet_pct=spec.sizing.pct if spec.sizing.pct is not None else settings.portfolio.position_pct,
            stress=(prev.get("stress") or {}).get(f"{sid}@{v}"),
        ))
        # credit the changes that produced a thriving strategy (once per version per day)
        last = [e for e in store.evo_rows(version=v, event="thriving") if now - e["ts"] < 86400]
        born = store.evo_rows(version=v, event="born")
        if born and not last:
            store.insert_evo(dict(ts=now, gen=gen, strategy_id=sid, version=v, parent_version=born[0]["parent_version"],
                                  mate_version=None, ops=born[0]["ops"], event="thriving", fitness=None,
                                  parent_fitness=None, reason=f"live wallet {equity:.0%} over {len(tr)} trades"))
        log_fn(f"winner {sid}@{v}: wallet {equity:.0%}, {len(tr)} trades, "
               f"keepers: {', '.join(k['feature'] + ' ' + k['op'] + ' ' + format(k['value'], 'g') for k in winners[-1]['keepers'][:2]) or 'none'}")
    out = dict(gen=gen, ts=now, winners=winners, stress=dict(prev.get("stress") or {}))
    if do_stress and winners:
        specs = [spec_from_json(store.get_version(w["strategy_id"], w["version"])["spec_json"]) for w in winners]
        live = {w["key"]: tr for w, (_, _, tr) in zip(winners, cands[:TOP])}
        res = stress(settings, specs, events_fn, window, live)
        for w in winners:
            out["stress"][w["key"]] = dict(res[w["key"]], gen=gen, ts=now)
            w["stress"] = out["stress"][w["key"]]
            log_fn(f"stress {w['key']}: robustness {w['stress']['robustness']:.0%}, "
                   f"live P(profit) {w['stress']['live_p_profit']:.0%}")
    save(settings.data_dir, out)
    return out
