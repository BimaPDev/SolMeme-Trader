"""Strategy swarm: a population of paper strategies that evolves. No LLM is involved.

Every generation (`solmeme evolve`, which `run` schedules every `evolution.every_min`):

  1. judge   each live agent from its own paper trades since it was born. Dead when its wallet
             busts (below bust_frac of the start), when it is losing with kill_confidence after
             kill_min_trades (bootstrap of its mean trade %), when it sits in the bottom
             kill_bottom_frac with a losing record, or when it stays idle while others trade.
  2. learn   losing trades become lessons: "don't buy when <feature> <op> <value>" cuts that would
             have removed the most losing SOL. Every mutation's outcome (screened out, born, killed,
             promoted) is credited to its kind, so mutations that keep failing are tried less.
  3. breed   tournament-select parents (live score once an agent has trades, replay score before),
             cross two agents of the same strategy, then mutate through strategy.diff, which keeps
             the same guardrails as AI proposals: param bounds, locked rules, fixed universe. The one
             extra gene is bet size (sizing.pct, 5-35% of the wallet), which the AI can never change.
  4. screen  replay the newest window_h of recorded events walk-forward (folds); only offspring at least as
             good as their parent and the swarm's worst survivor go live, each on its own paper wallet.
  5. promote an agent that beats its strategy's champion on live trades (bootstrap CI > 0) takes
             the champion's place on the shared wallet.
"""

from __future__ import annotations

import json
import math
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable

import numpy as np

from solmeme.config import Settings
from solmeme.core.store import Store
from solmeme.lab.metrics import bootstrap_diff_ci, compute
from solmeme.lab.mining import _feat_matrix
from solmeme.lab.optimize import score
from solmeme.sim.replay import run_replay
from solmeme.strategy.diff import EXIT_BOUNDS, Change, InvalidChange, apply_changes
from solmeme.strategy.spec import StrategySpec, spec_from_json

SWARM = "swarm"
DEAD = "dead"
SIZING_PCT_BOUNDS = (5.0, 35.0)   # the swarm's bet-size gene: % of the agent's wallet per buy
BOOST_X_BOUNDS = (1.1, 2.5)       # whale boost: bet this many times the normal size
BOOST_WHALES_MAX = 3
KILL_PENALTY = -15.0
THRIVE_BONUS = 15.0           # credit for a mutation whose agent became a live winner (lab/winners.py)          # credit for a mutation whose agent later died (score units ≈ % per trade)
EventsFn = Callable[[float, float], Iterable]


# ── fitness ────────────────────────────────────────────────────────────────
@dataclass
class Fit:
    replay: float = -100.0
    replay_n: int = 0
    live: float | None = None
    live_n: int = 0
    live_pnl_sol: float = 0.0
    equity_frac: float = 1.0
    live_pcts: list = field(default_factory=list)

    def blended(self, min_live: int) -> float:
        """Replay score until live trades exist, then live results take over."""
        if self.live is None or self.live_n == 0:
            return self.replay
        w = self.live_n / (self.live_n + min_live)
        return w * self.live + (1 - w) * self.replay


def recorded_folds(events_dir, hours: float, folds: int, now: float | None = None) -> list[tuple[float, float]]:
    """The newest `hours` of hourly event files that exist, split into `folds` time-ordered chunks.
    Gaps (the bot was off) are skipped, so a week-old recording still counts when nothing newer exists."""
    from datetime import datetime, timezone

    from solmeme.core.eventlog import list_files

    now = now or time.time()
    starts = set()
    for f in list_files(events_dir):
        try:
            t = datetime.strptime(f.parent.name + f.name[:2], "%Y%m%d%H").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if t <= now:
            starts.add(t)
    hrs = sorted(starts)[-max(1, int(round(hours))):]
    if not hrs:
        return [(now - hours * 3600, now)]
    k = max(1, min(folds, len(hrs)))
    chunks = [hrs[len(hrs) * i // k: len(hrs) * (i + 1) // k] for i in range(k)]
    return [(c[0], min(c[-1] + 3600, now)) for c in chunks if c]


def replay_fitness(settings: Settings, specs: list[StrategySpec], events_fn: EventsFn,
                   folds: list[tuple[float, float]], shrink_trades: int) -> tuple[dict[str, Fit], list[dict]]:
    """Median fold score per spec (optimize.score: expectancy shrunk toward 0 below shrink_trades,
    outlier penalty), plus every replay trade with its entry features (the raw material for lessons)."""
    fold_scores: dict[str, list[float]] = defaultdict(list)
    counts: dict[str, int] = defaultdict(int)
    trades: list[dict] = []
    per_fold_min = max(3, shrink_trades // len(folds))
    for a, b in folds:
        eng = run_replay(settings, [(s, "candidate") for s in specs], events_fn(a, b),
                         snapshot_features=True, record_near_misses=False)
        grouped: dict[str, list[dict]] = defaultdict(list)
        for t in eng.journal.trades:
            grouped[f"{t['strategy_id']}@{t['version']}"].append(t)
        trades += eng.journal.trades
        for r in eng.runners:
            m = compute(grouped.get(r.key, []), r.key, r.failed_buys, r.failed_buy_cost)
            fold_scores[r.key].append(score(m, per_fold_min))
            counts[r.key] += m.n_trades
    return {k: Fit(replay=float(np.median(v)), replay_n=counts[k]) for k, v in fold_scores.items()}, trades


# ── lessons: entry conditions that lost money ─────────────────────────────
@dataclass
class Lesson:
    feature: str
    op: str            # the rule goes in entry.none: block the token when `feature op v`
    v: float
    saved_sol: float   # losing SOL the cut would have removed, net of winners it also blocks
    blocked: int
    of: int

    def text(self) -> str:
        return f"don't buy when {self.feature} {self.op} {self.v:g} (saves {self.saved_sol:+.3f} SOL, blocks {self.blocked}/{self.of})"


def learn_lessons(trades: list[dict], min_trades: int = 12, top: int = 8) -> list[Lesson]:
    """For each numeric entry feature, the threshold cut whose blocked trades lost the most SOL.
    Keeps at least 30% of trades so a lesson never just means 'stop trading'."""
    if len(trades) < min_trades:
        return []
    pnl = np.array([float(t["pnl_sol"]) for t in trades])
    names, X = _feat_matrix(trades, drop_market=True)
    out: list[Lesson] = []
    for j, name in enumerate(names):
        col = X[:, j]
        mask = ~np.isnan(col)
        if mask.sum() < min_trades or np.nanstd(col) == 0:
            continue
        best: Lesson | None = None
        for q in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
            t = float(np.quantile(col[mask], q))
            for op, blocked in ((">", mask & (col > t)), ("<", mask & (col < t))):
                nb = int(blocked.sum())
                if nb < 3 or len(trades) - nb < 0.3 * len(trades):
                    continue
                saved = -float(pnl[blocked].sum())
                if saved > 0 and (best is None or saved > best.saved_sol):
                    best = Lesson(name, op, round(t, 6), saved, nb, len(trades))
        if best:
            out.append(best)
    out.sort(key=lambda l: -l.saved_sol)
    return out[:top]


# ── mutation credit: stop retrying what keeps failing ─────────────────────
class Credit:
    """Per strategy and mutation kind: mean fitness change of children over parents, with deaths
    counted as KILL_PENALTY. Kinds with 3+ outcomes that never helped become near-taboo."""

    def __init__(self, rows: list[dict]):
        self.deltas: dict[tuple[str, str], list[float]] = defaultdict(list)
        for r in rows:
            for sig in json.loads(r["ops"] or "[]"):
                if r["event"] == "killed":
                    self.deltas[(r["strategy_id"], sig)].append(KILL_PENALTY)
                elif r["event"] == "thriving":
                    self.deltas[(r["strategy_id"], sig)].append(THRIVE_BONUS)
                elif r["fitness"] is not None and r["parent_fitness"] is not None:
                    self.deltas[(r["strategy_id"], sig)].append(r["fitness"] - r["parent_fitness"])

    def weight(self, strategy_id: str, sig: str) -> float:
        d = self.deltas.get((strategy_id, sig))
        if not d:
            return 1.0
        if len(d) >= 3 and max(d) <= 0:
            return 0.05
        return float(min(3.0, max(0.2, math.exp(float(np.mean(d)) / 10))))

    def table(self, strategy_id: str | None = None) -> list[dict]:
        rows = []
        for (sid, sig), d in self.deltas.items():
            if strategy_id and sid != strategy_id:
                continue
            rows.append(dict(strategy_id=sid, kind=sig, n=len(d), mean_delta=float(np.mean(d)),
                             weight=self.weight(sid, sig)))
        return sorted(rows, key=lambda r: -r["mean_delta"])


def _kind(sig: str) -> str:
    """Coarse family of a signature, for weighting which kind of mutation to try next."""
    return sig.split(":", 1)[0]


# ── variation ──────────────────────────────────────────────────────────────
@dataclass
class Child:
    spec: StrategySpec
    parent: StrategySpec
    mate: StrategySpec | None
    sigs: list[str] = field(default_factory=list)


class Breeder:
    def __init__(self, rng: random.Random, credit: Credit, feature_values: dict[str, np.ndarray],
                 lessons: dict[str, list[Lesson]], max_mutations: int = 3, default_pct: float | None = None,
                 ideas: dict[str, list[dict]] | None = None, winners: list[dict] | None = None):
        self.default_pct = default_pct
        self.winners = winners or []
        self.ideas = ideas or {}
        self.rng = rng
        self.credit = credit
        self.values = feature_values
        self.lessons = lessons
        self.max_mutations = max_mutations

    # each proposer returns (signature, change) or None when it does not apply to this spec
    def _param(self, s: StrategySpec):
        if not s.params:
            return None
        k = self.rng.choice(sorted(s.params))
        p = s.params[k]
        v = p.value + self.rng.gauss(0, 0.15 * (p.hi - p.lo))
        v = min(p.hi, max(p.lo, v))
        if p.type == "int":
            v = float(round(v))
        if v == p.value:
            return None
        return f"param:{k}:{'+' if v > p.value else '-'}", Change(op="set_param", param=k, value=v)

    def _rule_value(self, s: StrategySpec):
        rules = [c for _, c in s.entry.rules() if not c.locked and isinstance(c.v, (int, float))
                 and not isinstance(c.v, bool) and c.v != 0]
        if not rules:
            return None
        c = self.rng.choice(rules)
        v = round(float(c.v) * math.exp(self.rng.gauss(0, 0.3)), 6)
        return f"rule_value:{c.id}:{'+' if v > c.v else '-'}", Change(op="set_rule_value", rule_id=c.id, value=v)

    def _add_rule(self, s: StrategySpec):
        used = {c.f for _, c in s.entry.rules()}
        pool = [f for f in sorted(self.values) if f not in used]
        if not pool:
            return None
        f = self.rng.choice(pool)
        op = self.rng.choice([">=", "<="])
        v = round(float(np.quantile(self.values[f], self.rng.uniform(0.15, 0.85))), 6)
        return f"add_rule:{f}:{op}", Change(op="add_rule", section="all", rule_id=f"evo_{f}", feature=f,
                                              operator=op, value=v)

    def _remove_rule(self, s: StrategySpec):
        removable = [(sec, c) for sec, c in s.entry.rules() if not c.locked]
        positive = [1 for sec, _ in s.entry.rules() if sec in ("all", "any")]
        if not removable:
            return None
        sec, c = self.rng.choice(removable)
        if sec in ("all", "any") and len(positive) <= 1:
            return None            # never strip the last entry condition (it would buy everything)
        return f"remove_rule:{c.id}", Change(op="remove_rule", rule_id=c.id)

    def _exit(self, s: StrategySpec):
        f = self.rng.choice(sorted(EXIT_BOUNDS))
        cur = s.p(getattr(s.exits, f))
        lo, hi = EXIT_BOUNDS[f]
        if cur is None:
            v = {"stop_loss_pct": 40, "trailing_pct": 30, "max_hold_s": 1800, "stale_after_s": 300}.get(f, 1.5)
        elif f == "trailing_activate_x":
            v = 1 + (float(cur) - 1) * math.exp(self.rng.gauss(0, 0.35))
        else:
            v = float(cur) * math.exp(self.rng.gauss(0, 0.3))
        v = round(min(hi, max(lo, v)), 3)
        ref = getattr(s.exits, f)
        if isinstance(ref, str) and ref.startswith("$"):     # bounded by the param, not the sanity bounds
            p = s.params[ref[1:]]
            v = min(p.hi, max(p.lo, v))
        if cur is not None and v == float(cur):
            return None
        return f"exit:{f}:{'+' if cur is None or v > float(cur) else '-'}", Change(op="set_exit", exit_field=f, value=v)

    def _take_profit(self, s: StrategySpec):
        ladder = [[float(s.p(t.at_x)), float(s.p(t.sell_pct))] for t in s.exits.take_profit] or [[2.0, 50.0], [4.0, 100.0]]
        out = [[round(min(100, max(1.05, x * math.exp(self.rng.gauss(0, 0.2)))), 3),
                round(min(100, max(1, pct + self.rng.gauss(0, 15))), 1)] for x, pct in ladder]
        out.sort()
        out[-1][1] = 100.0 if ladder[-1][1] >= 100 else out[-1][1]
        up = sum(x for x, _ in out) > sum(x for x, _ in ladder)
        return f"take_profit:{'+' if up else '-'}", Change(op="set_take_profit", take_profit=out)

    def _lesson(self, s: StrategySpec):
        used = {c.f for _, c in s.entry.rules()}
        options = [l for l in self.lessons.get(s.id, []) if l.feature not in used]
        if not options:
            return None
        l = self.rng.choices(options, weights=[max(1e-6, x.saved_sol) for x in options])[0]
        return f"lesson:{l.feature}:{l.op}", Change(op="add_rule", section="none", rule_id=f"lesson_{l.feature}",
                                                    feature=l.feature, operator=l.op, value=l.v)

    def lesson_variants(self, s: StrategySpec, l: Lesson, spread: float = 0.1) -> list[tuple[StrategySpec, list[str]]]:
        """One lesson at three strengths on the same parent: the cut where it was learned, and moved
        `spread` of the observed values each way. Screening then picks how hard to apply it, instead of
        waiting generations for the lesson to come up in a random draw."""
        cuts = {float(l.v)}
        vals = self.values.get(l.feature)
        if vals is not None and len(vals):
            q = float((vals < l.v).mean())
            cuts |= {float(np.quantile(vals, min(1.0, max(0.0, q + d)))) for d in (-spread, spread)}
        out = []
        for v in sorted(cuts):
            ch = Change(op="add_rule", section="none", rule_id=f"lesson_{l.feature}", feature=l.feature,
                        operator=l.op, value=round(v, 6))
            try:
                child = apply_changes(s, [ch], max_changes=self.max_mutations)
            except InvalidChange:
                continue
            if child.version != s.version:
                out.append((child, [f"lesson:{l.feature}:{l.op}", "lesson_fanout"]))
        return out

    def _idea(self, s: StrategySpec):
        """A council idea (approved, still testing or working) as an entry rule."""
        used = {c.f for _, c in s.entry.rules()}
        options = [i for i in self.ideas.get(s.id, []) if i["feature"] not in used]
        if not options:
            return None
        i = self.rng.choice(options)
        return f"idea:{i['idea_id']}", Change(op="add_rule", section="all" if i["kind"] == "require" else "none",
                                              rule_id=f"idea_{i['feature']}", feature=i["feature"],
                                              operator=i["op"], value=i["v"])

    def _winner_rule(self, s: StrategySpec):
        """An entry condition a live winner's winning trades shared, for agents of the same strategy."""
        used = {c.f for _, c in s.entry.rules()}
        options = [k for w in self.winners if w["strategy_id"] == s.id for k in w.get("keepers") or []
                   if k["feature"] not in used]
        if not options:
            return None
        k = self.rng.choices(options, weights=[max(1e-6, x["saves_sol"]) for x in options])[0]
        return f"winner_rule:{k['feature']}:{k['op']}", Change(op="add_rule", section="all", rule_id=f"win_{k['feature']}",
                                                               feature=k["feature"], operator=k["op"], value=k["value"])

    def _adopt_exits(self, s: StrategySpec):
        """A live winner's take-profit ladder, for agents of any strategy (exits travel well)."""
        options = [w for w in self.winners if w.get("ladder") and w["key"] != s.key]
        if not options:
            return None
        w = self.rng.choices(options, weights=[max(1e-6, x["equity"]) for x in options])[0]
        mine = [[float(s.p(t.at_x)), float(s.p(t.sell_pct))] for t in s.exits.take_profit]
        if mine == w["ladder"]:
            return None
        return f"adopt_exits:{w['strategy_id']}", Change(op="set_take_profit", take_profit=w["ladder"])

    def _sizing(self, s: StrategySpec):
        """Bet size gene. Not a strategy.diff Change on purpose: the AI may never touch sizing."""
        cur = s.sizing.pct if s.sizing.pct is not None else self.default_pct
        if not cur:
            return None
        lo, hi = SIZING_PCT_BOUNDS
        v = round(min(hi, max(lo, cur * math.exp(self.rng.gauss(0, 0.3)))), 1)
        if v == cur:
            return None
        return f"sizing_pct:{'+' if v > cur else '-'}", v

    def _boost(self, s: StrategySpec):
        """Whale bet-boost gene (sizing, so never an AI change): bet boost_x more when at least boost_whales
        trusted whales bought the token in the last 10 minutes. Replays cannot see whales (that would be
        look-ahead), so a boost-only child scores like its parent there and is judged by its live trades."""
        z = s.sizing
        lo, hi = BOOST_X_BOUNDS
        if not z.boost_whales:
            return "boost:add", (1, round(self.rng.uniform(1.25, 2.0), 2))
        roll = self.rng.random()
        if roll < 0.2:
            return "boost:drop", (None, 1.0)
        if roll < 0.5:
            w = min(BOOST_WHALES_MAX, max(1, z.boost_whales + self.rng.choice([-1, 1])))
            return (f"boost:whales{'+' if w > z.boost_whales else '-'}", (w, z.boost_x)) if w != z.boost_whales else None
        x = round(min(hi, max(lo, z.boost_x * math.exp(self.rng.gauss(0, 0.2)))), 2)
        return (f"boost:x{'+' if x > z.boost_x else '-'}", (z.boost_whales, x)) if x != z.boost_x else None

    KINDS = ("param", "rule_value", "add_rule", "remove_rule", "exit", "take_profit", "lesson", "sizing", "idea",
             "winner_rule", "adopt_exits", "boost")
    BASE = {"param": 3.0, "rule_value": 1.5, "add_rule": 1.0, "remove_rule": 0.7, "exit": 2.0,
            "take_profit": 1.0, "lesson": 2.0, "sizing": 1.0, "idea": 3.0, "winner_rule": 2.5, "adopt_exits": 1.5,
            "boost": 1.0}

    def mutate(self, s: StrategySpec) -> tuple[StrategySpec, list[str]] | None:
        n = self.rng.randint(1, self.max_mutations)
        proposers = {k: getattr(self, "_" + k) for k in self.KINDS}
        changes: list[Change] = []
        sigs: list[str] = []
        new_pct: float | None = None
        new_boost: tuple | None = None
        for _ in range(n * 4):
            if len(changes) + (new_pct is not None) + (new_boost is not None) >= n:
                break
            kinds = list(self.KINDS)
            w = [self.BASE[k] * self._kind_weight(s.id, k) for k in kinds]
            kind = self.rng.choices(kinds, weights=w)[0]
            got = proposers[kind](s)
            if got is None:
                continue
            sig, ch = got
            if self.credit.weight(s.id, sig) < 0.1 and self.rng.random() > 0.1:
                continue          # this exact move kept failing: skip it nine times in ten
            if kind == "sizing":
                if new_pct is None:
                    new_pct = ch
                    sigs.append(sig)
                continue
            if kind == "boost":
                if new_boost is None:
                    new_boost = ch
                    sigs.append(sig)
                continue
            if any(_target(ch) == _target(c) for c in changes):
                continue
            try:
                apply_changes(s, [*changes, ch], max_changes=self.max_mutations)
            except InvalidChange:
                continue
            changes.append(ch)
            sigs.append(sig)
        if not changes and new_pct is None and new_boost is None:
            return None
        child = apply_changes(s, changes, max_changes=self.max_mutations) if changes else s
        if new_pct is not None or new_boost is not None:
            d = child.model_dump(mode="json")
            if new_pct is not None:
                d["sizing"]["pct"] = new_pct
            if new_boost is not None:
                d["sizing"]["boost_whales"], d["sizing"]["boost_x"] = new_boost
            child = StrategySpec.model_validate(d)
        return (child, sigs) if child.version != s.version else None

    def _kind_weight(self, sid: str, kind: str) -> float:
        ws = [self.credit.weight(s, sig) for (s, sig) in self.credit.deltas if s == sid and _kind(sig) == kind]
        return float(np.mean(ws)) if ws else 1.0

    def crossover(self, a: StrategySpec, b: StrategySpec) -> StrategySpec | None:
        """Uniform crossover of two agents of the same strategy. Universe and sizing come from a,
        locked rules always survive."""
        if a.id != b.id or a.version == b.version:
            return None
        da, db = a.model_dump(mode="json"), b.model_dump(mode="json")
        child = json.loads(json.dumps(da))
        for k in child["params"]:
            if k in db["params"] and self.rng.random() < 0.5:
                child["params"][k] = db["params"][k]
        for k in child["exits"]:
            if self.rng.random() < 0.5:
                child["exits"][k] = db["exits"][k]
        if self.rng.random() < 0.5:
            child["sizing"]["pct"] = db["sizing"].get("pct")     # bet size is the only sizing gene
        for sec in ("all", "any", "none"):
            ra = {r["id"]: r for r in da["entry"][sec]}
            rb = {r["id"]: r for r in db["entry"][sec]}
            rules = []
            for rid in list(ra) + [r for r in rb if r not in ra]:
                x, y = ra.get(rid), rb.get(rid)
                if x and y:
                    rules.append(x if self.rng.random() < 0.5 else y)
                elif (x or y).get("locked") or self.rng.random() < 0.5:
                    rules.append(x or y)
            child["entry"][sec] = rules
        if not (child["entry"]["all"] or child["entry"]["any"]):
            return None
        try:
            spec = StrategySpec.model_validate(child)
        except Exception:  # noqa: BLE001
            return None
        return None if spec.version in (a.version, b.version) else spec


def _target(c: Change) -> str:
    return f"{c.op}:{c.param or c.rule_id or c.exit_field or c.feature or ''}"


def feature_values(trades: list[dict], min_n: int = 20) -> dict[str, np.ndarray]:
    """Observed entry values per numeric feature, the range new rules draw thresholds from."""
    if not trades:
        return {}
    names, X = _feat_matrix(trades, drop_market=True)
    out = {}
    for j, n in enumerate(names):
        col = X[:, j][~np.isnan(X[:, j])]
        if len(col) >= min_n and np.std(col) > 0 and not set(np.unique(col)) <= {0.0, 1.0}:
            out[n] = col
    return out


# ── the generation ─────────────────────────────────────────────────────────
@dataclass
class Agent:
    spec: StrategySpec
    status: str
    born_ts: float
    fit: Fit = field(default_factory=Fit)


@dataclass
class GenerationReport:
    gen: int
    window: tuple[float, float]
    killed: list[tuple[str, str]] = field(default_factory=list)
    born: list[tuple[str, list[str], float, float]] = field(default_factory=list)
    screened_out: int = 0
    promoted: list[tuple[str, str]] = field(default_factory=list)
    board: list[dict] = field(default_factory=list)
    lessons: dict[str, list[Lesson]] = field(default_factory=dict)
    credit: list[dict] = field(default_factory=list)
    council: list[dict] = field(default_factory=list)
    bans: list[dict] = field(default_factory=list)
    whales: list[dict] = field(default_factory=list)
    winners: list[dict] = field(default_factory=list)
    seconds: float = 0.0


def losing_upper_bound(pcts: list[float], conf: float, seed: str = "", iters: int = 4000) -> float:
    """Upper `conf` quantile of the bootstrapped mean trade %: below 0 means the agent is losing
    with that confidence (a short run of pure losses is enough; a mixed record is not)."""
    rng = np.random.default_rng(int.from_bytes(seed.encode()[:8].ljust(8, b"0"), "big"))
    a = np.asarray(pcts, float)
    return float(np.quantile(rng.choice(a, (iters, len(a))).mean(axis=1), conf))


def _live_stats(store: Store, a: Agent, start_sol: float, min_trades: int, reset_ts: float = 0.0,
                trust_ts: float = 0.0) -> None:
    since = max(a.born_ts, trust_ts)          # trades filled by the broken model (wallet.trusted_since) don't count
    trades = [t for t in store.trades(a.spec.id, a.spec.version, run_mode="live") if (t["entry_ts"] or 0) >= since]
    a.fit.live_n = len(trades)
    a.fit.live_pcts = [float(t["pnl_pct"] or 0) for t in trades]
    a.fit.live_pnl_sol = sum(float(t["pnl_sol"]) for t in trades)
    # the wallet (bust line) counts from the last wallet reset; the track record above keeps everything
    wallet_pnl = sum(float(t["pnl_sol"]) for t in trades if (t["entry_ts"] or 0) >= reset_ts)
    a.fit.equity_frac = 1 + wallet_pnl / start_sol if start_sol else 1.0
    a.fit.live = score(compute(trades), min_trades) if trades else None


def evolve_once(settings: Settings, store: Store, events_fn: EventsFn, now: float | None = None,
                rng: random.Random | None = None, start_sol: float | None = None,
                folds: list[tuple[float, float]] | None = None,
                phase_fn: Callable[[str], None] = lambda _: None,
                log_fn: Callable[[str], None] = lambda _: None) -> GenerationReport:
    from solmeme.ai.promoter import Promoter

    cfg = settings.evolution
    t0 = time.time()
    now = now or time.time()
    rng = rng or random.Random(cfg.seed if cfg.seed is not None else int(now))
    start_sol = start_sol or settings.portfolio.starting_sol
    gen = store.evo_generation() + 1
    if folds is None:            # plain clock window, split evenly
        a = now - cfg.window_h * 3600
        folds = [(a + (now - a) * i / cfg.folds, a + (now - a) * (i + 1) / cfg.folds) for i in range(cfg.folds)]
    window = (folds[0][0], folds[-1][1])
    rep = GenerationReport(gen=gen, window=window)
    rs = settings.model_copy(deep=True)       # replays fund each agent like the live swarm wallet
    rs.portfolio.starting_sol = start_sol

    # population: champions/trials are the seeds of each species, swarm agents compete live
    seeds: dict[str, StrategySpec] = {}
    agents: list[Agent] = []
    for row in store.active_versions():
        spec = spec_from_json(row["spec_json"])
        if spec.id in cfg.species_exclude:
            continue
        if row["status"] in ("champion", "trial"):
            if row["status"] == "champion" or spec.id not in seeds:
                seeds[spec.id] = spec
        elif row["status"] == SWARM:
            agents.append(Agent(spec, SWARM, row["status_ts"]))

    phase_fn("judging")
    # 1. judge (live paper trades since birth)
    from solmeme.sim.wallet import trusted_since, wallet_reset_ts
    reset_ts, trust_ts = wallet_reset_ts(settings), trusted_since(settings)
    for a in agents:
        _live_stats(store, a, start_sol, cfg.min_live_trades, reset_ts, trust_ts)
    all_live = sum(a.fit.live_n for a in agents)
    dead: list[tuple[Agent, str]] = []
    for a in agents:
        if a.fit.equity_frac < cfg.bust_frac:
            dead.append((a, f"busted: wallet at {a.fit.equity_frac:.0%} of start after {a.fit.live_n} trades"))
        elif (a.fit.live_n == 0 and now - a.born_ts > cfg.idle_kill_h * 3600
              and all_live >= 3 * cfg.min_live_trades):
            dead.append((a, f"idle: no trades in {(now - a.born_ts) / 3600:.0f}h while the swarm made {all_live}"))
    for a in agents:
        if a.fit.live_n < cfg.kill_min_trades or any(d is a for d, _ in dead):
            continue
        hi = losing_upper_bound(a.fit.live_pcts, cfg.kill_confidence, seed=a.spec.version)
        if hi < 0:
            mean = float(np.mean(a.fit.live_pcts))
            dead.append((a, f"losing: {a.fit.live_n} trades, {sum(p > 0 for p in a.fit.live_pcts)} won, "
                            f"{mean:+.1f}% a trade (at best {hi:+.1f}% with {cfg.kill_confidence:.0%} confidence)"))
    dead_keys = {a.spec.key for a, _ in dead}
    judged = sorted([a for a in agents if a.fit.live_n >= cfg.min_live_trades and a.spec.key not in dead_keys],
                    key=lambda a: a.fit.live or 0.0)
    n_bottom = int(len(judged) * cfg.kill_bottom_frac)
    for a in judged[:n_bottom]:
        if a.fit.live_pnl_sol < 0:
            dead_keys.add(a.spec.key)
            dead.append((a, f"bottom {cfg.kill_bottom_frac:.0%}: {a.fit.live_n} trades, {a.fit.live_pnl_sol:+.3f} SOL"))
    # turnover: a swarm full of winners never loses anyone, so it stops trying anything new. When fewer
    # than min_free_slots would open, the weakest judged agents (never the top half) retire to make room.
    # Retiring is not failing: its mutations keep their credit and its council ideas keep their record.
    retiring: list[tuple[Agent, str]] = []
    free = cfg.population - (len(agents) - len(dead_keys))
    if cfg.min_free_slots > 0 and free < cfg.min_free_slots:
        ranked = [a for a in judged if a.spec.key not in dead_keys]          # weakest first
        for a in ranked[: max(0, len(ranked) - (len(ranked) + 1) // 2)][: cfg.min_free_slots - free]:
            dead_keys.add(a.spec.key)
            retiring.append((a, f"retired to make room: weakest of {len(ranked)} judged agents "
                                f"(fitness {a.fit.live or 0.0:+.1f}, {a.fit.live_n} trades, {a.fit.live_pnl_sol:+.3f} SOL)"))
    for a, why in retiring:
        store.set_status(a.spec.id, a.spec.version, DEAD, note=why)
        born = store.evo_rows(version=a.spec.version, event="born")
        store.insert_evo(dict(ts=now, gen=gen, strategy_id=a.spec.id, version=a.spec.version,
                              parent_version=born[0]["parent_version"] if born else None, mate_version=None,
                              ops=born[0]["ops"] if born else "[]", event="retired", fitness=a.fit.live,
                              parent_fitness=None, reason=why))
        rep.killed.append((a.spec.key, why))
        log_fn(f"retired {a.spec.key}: {why}")
    for a, why in dead:
        store.set_status(a.spec.id, a.spec.version, DEAD, note=why)
        born = store.evo_rows(version=a.spec.version, event="born")
        store.insert_evo(dict(ts=now, gen=gen, strategy_id=a.spec.id, version=a.spec.version,
                              parent_version=born[0]["parent_version"] if born else None, mate_version=None,
                              ops=born[0]["ops"] if born else "[]", event="killed", fitness=a.fit.live,
                              parent_fitness=None, reason=why))
        rep.killed.append((a.spec.key, why))
        log_fn(f"killed {a.spec.key}: {why}")
    alive = [a for a in agents if a.spec.key not in dead_keys]

    # 5. promote (before breeding, so the winner's line seeds the next children)
    from solmeme.lab import winners as winlab
    win_book = winlab.load(settings.data_dir)
    if cfg.auto_promote:
        promoter = Promoter(settings, store)
        for a in alive:
            if a.fit.live_n < settings.promotion.min_shadow_trades:
                continue
            since = max(a.born_ts, trust_ts)
            ours = [float(t["pnl_sol"]) for t in store.trades(a.spec.id, a.spec.version, run_mode="live")
                    if (t["entry_ts"] or 0) >= since]
            champ = store.champion(a.spec.id)
            theirs = [float(t["pnl_sol"]) for t in store.trades(a.spec.id, champ["version"], run_mode="live")
                      if (t["entry_ts"] or 0) >= since] if champ else []
            base = theirs if len(theirs) >= 5 else [0.0] * 5
            lo, point, hi = bootstrap_diff_ci(ours, base, settings.promotion.bootstrap_iters,
                                              settings.promotion.bootstrap_conf)
            robust = ((win_book.get("stress") or {}).get(a.spec.key) or {}).get("robustness")
            if lo > 0 and np.mean(ours) > 0 and cfg.stress_tests and (robust is None or robust < cfg.min_robustness):
                log_fn(f"not promoting {a.spec.key} yet: robustness "
                       f"{'not tested' if robust is None else f'{robust:.0%}'} < {cfg.min_robustness:.0%}")
                continue
            if lo > 0 and np.mean(ours) > 0:
                why = f"swarm gen {gen}: +{point:.4f} SOL/trade over champion (CI {lo:+.4f}..{hi:+.4f}, {len(ours)} trades)"
                promoter.promote(a.spec.id, a.spec.version, why)
                store.insert_evo(dict(ts=now, gen=gen, strategy_id=a.spec.id, version=a.spec.version,
                                      parent_version=None, mate_version=None, ops="[]", event="promoted",
                                      fitness=a.fit.live, parent_fitness=None, reason=why))
                rep.promoted.append((a.spec.key, why))
                seeds[a.spec.id] = a.spec
                log_fn(f"promoted {a.spec.key}: {why}")
        alive = [a for a in alive if store.get_version(a.spec.id, a.spec.version)["status"] == SWARM]

    phase_fn("learning")
    # 2. learn from the population's own trades: replay of seeds + survivors, plus live losers
    pop = list(seeds.values()) + [a.spec for a in alive]
    fits, trades = replay_fitness(rs, pop, events_fn, folds, cfg.shrink_trades)
    for a in alive:
        f = fits.get(a.spec.key, Fit())
        a.fit.replay, a.fit.replay_n = f.replay, f.replay_n
    live_rows = [t for sid in seeds for t in store.trades(sid, run_mode="live", since=window[0])]
    by_species: dict[str, list[dict]] = defaultdict(list)
    for t in trades + live_rows:
        by_species[t["strategy_id"]].append(t)
    rep.lessons = {sid: learn_lessons(ts) for sid, ts in by_species.items()}
    for sid, ls in rep.lessons.items():
        for l in ls:
            store.insert_evo_lesson(dict(gen=gen, ts=now, strategy_id=sid, feature=l.feature, op=l.op, v=l.v,
                                         saved_sol=l.saved_sol, blocked=l.blocked, n=l.of))
    # council ideas so far, judged by their carriers (this generation's sitting runs after breeding,
    # so dead agents' slots refill quickly; its ideas are bred from the next generation on)
    from solmeme.ai.council import judge_ideas, sit
    judge_ideas(store, settings, now)
    ideas = {sid: [i for i in store.council_ideas(strategy_id=sid, approved=True) if i["status"] in ("testing", "working")]
             for sid in seeds}
    phase_fn("learning from winners")
    win_book = winlab.update(settings, store, events_fn, window, start_sol, gen, log_fn=log_fn, now=now, do_stress=False)
    rep.winners = win_book.get("winners") or []
    credit = Credit(store.evo_rows())
    breeder = Breeder(rng, credit, feature_values(trades + live_rows), rep.lessons, cfg.max_mutations,
                      default_pct=settings.portfolio.position_pct, ideas=ideas, winners=rep.winners)

    phase_fn("breeding")
    # 3. breed into free slots
    slots = cfg.population - len(alive)
    parents = [(s, fits.get(s.key, Fit()).replay) for s in seeds.values()] + \
              [(a.spec, a.fit.blended(cfg.min_live_trades)) for a in alive]
    parents = [p for p in parents if p[1] > -100 or p[0].id in seeds]
    children: list[Child] = []
    seen = {s.version for s, _ in parents}
    per_species = defaultdict(int, {sid: sum(1 for a in alive if a.spec.id == sid) for sid in seeds})
    cap = max(1, math.ceil(cfg.population * cfg.max_species_share))
    elite_keys = {w["key"] for w in rep.winners}
    elite_queue = [s for s, _ in parents if s.key in elite_keys for _ in range(cfg.elite_children * 3)]
    elite_made: dict[str, int] = defaultdict(int)
    for _ in range(max(0, slots) * cfg.offspring_per_slot * 3 + len(elite_queue)):
        if len(children) >= max(0, slots) * cfg.offspring_per_slot + len(elite_keys) * cfg.elite_children or not parents:
            break
        elite_queue = [e for e in elite_queue if elite_made[e.key] < cfg.elite_children]
        if elite_queue:                       # winners first: they always get children
            p = elite_queue.pop(0)
        else:
            p, _ = max(rng.sample(parents, min(cfg.tournament, len(parents))), key=lambda x: x[1])
        if per_species[p.id] >= cap:
            continue
        mate = None
        base, sigs = p, []
        mates = [s for s, _ in parents if s.id == p.id and s.version != p.version]
        if mates and rng.random() < cfg.crossover_prob:
            mate = rng.choice(mates)
            x = breeder.crossover(p, mate)
            if x is not None:
                base, sigs = x, ["crossover"]
        got = breeder.mutate(base)
        if got is None:
            continue
        spec, msigs = got
        if spec.version in seen or store.get_version(spec.id, spec.version):
            continue          # already alive, dead or tried: evolution never repeats a genome
        seen.add(spec.version)
        if p.key in elite_keys:
            elite_made[p.key] += 1
        children.append(Child(spec, p, mate, sigs + msigs))

    # each species' strongest new lesson, at three strengths on its best parent that lacks it
    if getattr(cfg, "lesson_fanout", True):
        for sid, ls in rep.lessons.items():
            top = next((l for l in ls if l.saved_sol > 0), None)
            pool = [(sp, f) for sp, f in parents
                    if top and sp.id == sid and top.feature not in {c.f for _, c in sp.entry.rules()}]
            if not pool or per_species[sid] >= cap:
                continue
            base = max(pool, key=lambda x: x[1])[0]
            for spec, sigs in breeder.lesson_variants(base, top):
                if spec.version in seen or store.get_version(spec.id, spec.version):
                    continue
                seen.add(spec.version)
                children.append(Child(spec, base, None, sigs))

    phase_fn("screening")
    # 4. screen offspring on the same window, admit the best into free slots
    admitted: list[tuple[Child, Fit]] = []
    elite_admitted: dict[str, int] = defaultdict(int)
    if children:
        cfits, _ = replay_fitness(rs, [c.spec for c in children], events_fn, folds, cfg.shrink_trades)
        floor = min([a.fit.replay for a in alive], default=-100.0)
        ranked = sorted(children, key=lambda c: -cfits[c.spec.key].replay)
        for c in ranked:
            f = cfits[c.spec.key]
            parent_fit = fits.get(c.parent.key, Fit()).replay
            elite = c.parent.key in elite_keys
            bar = parent_fit - cfg.elite_margin if elite else max(floor, parent_fit)
            # a winner's children go live even when the swarm is full (kills trim it back)
            room = len(admitted) < slots or (elite and elite_admitted[c.parent.key] < cfg.elite_children)
            ok = (room and f.replay_n >= cfg.min_replay_trades and f.replay > -100
                  and f.replay >= bar and (per_species[c.spec.id] < cap or elite))
            row = dict(ts=now, gen=gen, strategy_id=c.spec.id, version=c.spec.version,
                       parent_version=c.parent.version, mate_version=c.mate.version if c.mate else None,
                       ops=json.dumps(c.sigs), event="born" if ok else "screened_out",
                       fitness=f.replay, parent_fitness=parent_fit,
                       reason=f"{f.replay_n} replay trades")
            store.insert_evo(row)
            if ok:
                if elite:
                    elite_admitted[c.parent.key] += 1
                store.upsert_version(c.spec.id, c.spec.version, c.spec.canonical_json(), SWARM,
                                     parent_version=c.parent.version,
                                     note=f"swarm gen {gen}: " + ", ".join(c.sigs))
                per_species[c.spec.id] += 1
                admitted.append((c, f))
                rep.born.append((c.spec.key, c.sigs, f.replay, parent_fit))
                log_fn(f"born {c.spec.key} from {c.parent.version}: {', '.join(c.sigs)} "
                       f"(replay {f.replay:+.1f} vs parent {parent_fit:+.1f})")
            else:
                rep.screened_out += 1

    # scoreboard
    for s in seeds.values():
        f = fits.get(s.key, Fit())
        rep.board.append(dict(key=s.key, role="champion" if store.champion(s.id) else "trial",
                              replay=f.replay, replay_n=f.replay_n, live_n=None, live_pnl_sol=None, equity=None,
                              fitness=f.replay))
    for a in alive:
        rep.board.append(dict(key=a.spec.key, role=SWARM, replay=a.fit.replay, replay_n=a.fit.replay_n,
                              live_n=a.fit.live_n, live_pnl_sol=a.fit.live_pnl_sol, equity=a.fit.equity_frac,
                              fitness=a.fit.blended(cfg.min_live_trades)))
    for c, f in admitted:
        rep.board.append(dict(key=c.spec.key, role="newborn", replay=f.replay, replay_n=f.replay_n, live_n=0,
                              live_pnl_sol=0.0, equity=1.0, fitness=f.replay))
    rep.board.sort(key=lambda r: -r["fitness"])
    for r in rep.board:
        store.insert_evo_score(dict(gen=gen, ts=now, key=r["key"], role=r["role"], replay=r["replay"],
                                    replay_n=r["replay_n"], live_n=r["live_n"], live_pnl_sol=r["live_pnl_sol"],
                                    equity=r["equity"], fitness=r["fitness"]))
    untested = any(not w.get("stress") for w in rep.winners)
    if cfg.stress_tests and rep.winners and (gen % max(1, cfg.stress_every) == 0 or untested):
        phase_fn("stress test")
        sw = (max(window[0], window[1] - cfg.stress_window_h * 3600), window[1])
        win_book = winlab.update(settings, store, events_fn, sw, start_sol, gen, log_fn=log_fn, now=now, do_stress=True)
        rep.winners = win_book.get("winners") or []
    phase_fn("ban scan")
    from solmeme.lab.banlist import update as update_bans
    rep.bans = update_bans(settings, store, events_fn, window, log_fn=log_fn, now=now)
    phase_fn("whale scan")
    from solmeme.lab.whales import chain_trace, update as update_whales
    rep.whales = update_whales(settings, events_fn, window, log_fn=log_fn, now=now,
                               trace=chain_trace if settings.evolution.trace_whales else None)
    if settings.council.enabled:            # the judges decide which whales the bot may trust (a few calls, not per trade)
        from solmeme.ai.council import review_whales
        try:
            review_whales(settings, store, log_fn=log_fn, now=now)
        except Exception as exc:  # noqa: BLE001 - a failed review never stops the generation
            log_fn(f"council: whale review failed: {exc}")
    if settings.council.enabled:
        phase_fn("council")
        rep.council = sit(settings, store, gen, seeds, by_species, log_fn=log_fn, now=now)
    rep.credit = Credit(store.evo_rows()).table()
    rep.seconds = time.time() - t0
    phase_fn("done")
    return rep


def report_markdown(rep: GenerationReport) -> str:
    def ts(x: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(x))

    out = [f"# Swarm generation {rep.gen}", "",
           f"Replay window {ts(rep.window[0])} → {ts(rep.window[1])} · took {rep.seconds:.0f}s", "",
           f"**{len(rep.born)} born · {len(rep.killed)} killed · {rep.screened_out} screened out · "
           f"{len(rep.promoted)} promoted**", ""]
    if rep.promoted:
        out += ["## Promoted", ""] + [f"- `{k}` — {why}" for k, why in rep.promoted] + [""]
    out += ["## Leaderboard", "", "| # | strategy | role | fitness | replay | replay trades | live trades | live PnL SOL | wallet |",
            "|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rep.board[:25], 1):
        live_n = "" if r["live_n"] is None else r["live_n"]
        pnl = "" if r["live_pnl_sol"] is None else f"{r['live_pnl_sol']:+.3f}"
        eq = "" if r["equity"] is None else f"{r['equity']:.0%}"
        out.append(f"| {i} | `{r['key']}` | {r['role']} | {r['fitness']:+.1f} | {r['replay']:+.1f} | {r['replay_n']} "
                   f"| {live_n} | {pnl} | {eq} |")
    out.append("")
    if rep.killed:
        out += ["## Killed", ""] + [f"- `{k}` — {why}" for k, why in rep.killed] + [""]
    if rep.born:
        out += ["## Born", ""] + [f"- `{k}` ({', '.join(sigs)}) — replay {f:+.1f} vs parent {pf:+.1f}"
                                  for k, sigs, f, pf in rep.born] + [""]
    lessons = [(sid, l) for sid, ls in rep.lessons.items() for l in ls[:3]]
    if lessons:
        out += ["## Lessons (what lost money)", ""] + [f"- **{sid}**: {l.text()}" for sid, l in lessons] + [""]
    if rep.bans:
        out += ["## Ban list", ""] + [f"- {'banned' if b['banned'] else 'not banned'} {len(b['wallets'])} wallet(s): {b['reason']}"
                                      + (f" (council: {b['council']})" if b.get("council") else "") for b in rep.bans] + [""]
    if rep.winners:
        out += ["## Winners (why they win)", ""]
        for w in rep.winners:
            st = w.get("stress") or {}
            out.append(f"- `{w['key']}`: wallet {w['equity']:.0%} over {w['trades']} trades"
                       + (f", robustness {st['robustness']:.0%}, live P(profit) {st['live_p_profit']:.0%}" if st else ""))
            for k in w.get("keepers") or []:
                out.append(f"  - winning trades shared `{k['feature']} {k['op']} {k['value']:g}` (keeps {k['keeps_profit']:.0%} of profit)")
        out.append("")
    if rep.whales:
        out += ["## Whales", ""] + [f"- `{c['wallet']}` {c['was']} → **{c['now']}** (score {c['score']}, early on {c['early_good']} good tokens)"
                                    for c in rep.whales] + [""]
    if rep.council:
        out += ["## Council", ""]
        for i in rep.council:
            verb = "buy only when" if i["kind"] == "require" else "don't buy when"
            out.append(f"- **{i['strategy_id']}** ({i['proposed_by']}): {verb} `{i['feature']} {i['op']} {i['v']:g}` — "
                       f"{i['status']}: {i['status_reason']}" + (f". Why: {i['reason']}" if i.get("reason") else ""))
        out.append("")
    if rep.credit:
        out += ["## What mutations have done", "", "| strategy | mutation | tries | mean Δfitness | weight |",
                "|---|---|---|---|---|"]
        for c in rep.credit[:12] + ([] if len(rep.credit) <= 18 else rep.credit[-6:]):
            out.append(f"| {c['strategy_id']} | {c['kind']} | {c['n']} | {c['mean_delta']:+.1f} | {c['weight']:.2f} |")
        out.append("")
    return "\n".join(out)
