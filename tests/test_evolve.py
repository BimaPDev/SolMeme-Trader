"""Strategy swarm: variation stays inside the guardrails, lessons and credit learn from failures,
and a generation kills, breeds and screens through the store the live session reads."""

import json
import random
import time

import numpy as np

from solmeme.ai.promoter import active_specs, ensure_seeded
from solmeme.core.store import Store
from solmeme.lab.evolve import Breeder, Credit, learn_lessons, evolve_once
from solmeme.sim.engine import Engine
from solmeme.strategy.spec import StrategySpec


def _breeder(seed=0, credit_rows=(), lessons=None):
    values = {"top10_pct": np.linspace(5, 80, 50), "holders": np.linspace(10, 500, 50)}
    return Breeder(random.Random(seed), Credit(list(credit_rows)), values, lessons or {}, max_mutations=3)


def test_mutations_stay_in_bounds_and_keep_locked_rules(specs):
    base = specs["dex_curve_momentum"]
    locked = {c.id for _, c in base.entry.rules() if c.locked}
    made = set()
    for seed in range(60):
        got = _breeder(seed).mutate(base)
        if got is None:
            continue
        child, sigs = got
        made.add(child.version)
        assert child.version != base.version and sigs
        assert child.id == base.id and child.universe == base.universe
        z, b = child.sizing, base.sizing                     # sizing: only the whale boost genes may move
        assert (z.sol, z.pct, z.max_open, z.max_entries_per_token) == (b.sol, b.pct, b.max_open, b.max_entries_per_token)
        if z.boost_whales:
            assert 1 <= z.boost_whales <= 3 and 1.1 <= z.boost_x <= 2.5
        assert locked <= {c.id for _, c in child.entry.rules() if c.locked}
        for k, p in child.params.items():
            assert p.lo <= p.value <= p.hi
        assert child.entry.all or child.entry.any
    assert len(made) > 30


def test_crossover_mixes_two_agents_of_one_strategy(specs):
    a = specs["dex_curve_momentum"]
    br = _breeder(1)
    b = a
    while sum(1 for k in a.params if a.params[k].value != b.params[k].value) < 2:
        b = br.mutate(b)[0]
    kids = [k for k in (br.crossover(a, b) for _ in range(20)) if k is not None]
    assert kids
    for k in kids:
        assert k.version not in (a.version, b.version)
        assert any(c.id == "rug" and c.locked for c in k.entry.none)
    assert br.crossover(a, specs["near_graduation"]) is None


def test_lessons_find_the_feature_that_lost_money():
    rng = np.random.default_rng(0)
    trades = []
    for _ in range(60):
        conc = float(rng.uniform(5, 80))
        trades.append({"pnl_sol": -0.05 if conc > 50 else 0.02, "pnl_pct": 0,
                       "entry_features": {"top10_pct": conc, "noise": float(rng.normal())}})
    lessons = learn_lessons(trades)
    assert lessons[0].feature == "top10_pct" and lessons[0].op == ">"
    assert 40 <= lessons[0].v <= 60 and lessons[0].saved_sol > 0


def test_lessons_become_none_rules(specs):
    base = specs["dex_curve_momentum"]
    from solmeme.lab.evolve import Lesson
    br = _breeder(2, lessons={base.id: [Lesson("top10_pct", ">", 45.0, 1.0, 10, 40)]})
    sig, ch = br._lesson(base)
    assert sig == "lesson:top10_pct:>" and ch.section == "none" and ch.value == 45.0


def test_credit_makes_repeated_failures_nearly_taboo():
    rows = [dict(strategy_id="s", ops=json.dumps(["param:sl:+"]), event="screened_out", fitness=-5, parent_fitness=0)
            for _ in range(3)]
    rows.append(dict(strategy_id="s", ops=json.dumps(["exit:trailing_pct:-"]), event="born", fitness=8,
                     parent_fitness=0))
    rows.append(dict(strategy_id="s", ops=json.dumps(["take_profit:+"]), event="killed", fitness=None,
                     parent_fitness=None))
    c = Credit(rows)
    assert c.weight("s", "param:sl:+") == 0.05
    assert c.weight("s", "exit:trailing_pct:-") > 1
    assert c.weight("s", "take_profit:+") < 1
    assert c.weight("other", "param:sl:+") == 1.0


def test_swarm_agents_trade_their_own_wallet(settings, specs):
    eng = Engine([(specs["dex_curve_momentum"], "champion"), (specs["near_graduation"], "swarm")], settings,
                 shared_wallet=True)
    champ, agent = eng.runners
    assert eng.in_shared_wallet(champ) and not eng.in_shared_wallet(agent)


def test_generation_breeds_then_kills_a_busted_agent(settings, events):
    settings.evolution.population = 4
    settings.evolution.window_h = 1.0
    settings.evolution.folds = 2
    settings.evolution.min_replay_trades = 1
    settings.evolution.seed = 3
    store = Store(settings.db_path)
    ensure_seeded(store, settings)
    now = events[-1].ts

    def events_fn(a, b):
        return [e for e in events if a <= e.ts < b]

    rep = evolve_once(settings, store, events_fn, now=now, start_sol=0.5)
    assert rep.gen == 1 and rep.born and len(rep.born) <= 4
    swarm = [(sp, lbl) for sp, lbl in active_specs(store) if lbl == "swarm"]
    assert len(swarm) == len(rep.born)
    assert {r["event"] for r in store.evo_rows()} >= {"born"}

    # one agent loses 90% of its wallet live → busted next generation, and its mutations are penalized
    victim = swarm[0][0]
    run = store.start_run("live")
    t = time.time() + 1
    for i in range(3):
        store.insert_trade(dict(trade_id=f"x{i}", run_id=run, strategy_id=victim.id, version=victim.version,
                                mint=f"m{i}", entry_ts=t, exit_ts=t + 1, pnl_sol=-0.15, pnl_pct=-60))
    rep2 = evolve_once(settings, store, events_fn, now=now, start_sol=0.5)
    assert rep2.gen == 2
    assert victim.key in [k for k, _ in rep2.killed]
    assert store.get_version(victim.id, victim.version)["status"] == "dead"
    assert victim.key not in {sp.key for sp, _ in active_specs(store)}
    killed = store.evo_rows(version=victim.version, event="killed")
    assert killed and json.loads(killed[0]["ops"])
    assert store.evo_scores()[0]["gen"] == 2


def test_folds_use_the_newest_recorded_hours_and_skip_gaps(tmp_path):
    from datetime import datetime, timezone

    from solmeme.lab.evolve import recorded_folds

    def hour(d, h):
        return datetime(2026, 10, d, h, tzinfo=timezone.utc).timestamp()

    for d, hs in ((1, range(0, 6)), (8, range(0, 3))):     # 6 hours on day 1, a week off, 3 on day 8
        (tmp_path / f"202610{d:02d}").mkdir()
        for h in hs:
            (tmp_path / f"202610{d:02d}" / f"{h:02d}.jsonl.gz").write_bytes(b"")
    now = hour(8, 2) + 1800
    folds = recorded_folds(tmp_path, hours=6, folds=3, now=now)
    assert folds == [(hour(1, 3), hour(1, 5)), (hour(1, 5), hour(8, 1)), (hour(8, 1), now)]


def test_swarm_view_lists_agents_generations_and_lessons(settings, events):
    from solmeme.ui.swarm_view import build_swarm

    settings.evolution.population = 3
    settings.evolution.window_h = 1.0
    settings.evolution.folds = 2
    settings.evolution.min_replay_trades = 1
    settings.evolution.seed = 3
    store = Store(settings.db_path)
    ensure_seeded(store, settings)
    rep = evolve_once(settings, store, lambda a, b: [e for e in events if a <= e.ts < b],
                      now=events[-1].ts, start_sol=0.5)
    view = build_swarm(store, settings, sol_usd=100.0)
    assert view["generation"] == 1 and view["generations"][0]["born"] == len(rep.born)
    keys = {a["key"] for a in view["agents"]}
    assert {k for k, *_ in rep.born} == keys
    agent = view["agents"][0]
    assert agent["status"] == "swarm" and agent["curve"][0][1] == 1.0 and agent["ops"]
    assert view["species"] and json.dumps(view)          # serializable for the website


def test_losing_with_confidence_needs_a_consistent_record():
    from solmeme.lab.evolve import losing_upper_bound
    assert losing_upper_bound([-30, -25, -40, -20], 0.8, "a") < 0          # four straight losses
    assert losing_upper_bound([-30, -25, 90, -20], 0.8, "a") > 0           # one runner keeps it alive
    assert losing_upper_bound([-5, -8, -3, 2, -6, -4, -7, 1], 0.8, "a") < 0


def test_a_consistent_loser_dies_before_eight_trades(settings, events):
    settings.evolution.population = 3
    settings.evolution.window_h = 1.0
    settings.evolution.folds = 2
    settings.evolution.min_replay_trades = 1
    settings.evolution.seed = 3
    store = Store(settings.db_path)
    ensure_seeded(store, settings)
    evolve_once(settings, store, lambda a, b: [e for e in events if a <= e.ts < b], now=events[-1].ts, start_sol=5.0)
    victim = next(sp for sp, lbl in active_specs(store) if lbl == "swarm")
    run = store.start_run("live")
    t = time.time() + 1
    for i in range(4):                                   # -4% of the wallet in total: nowhere near bust
        store.insert_trade(dict(trade_id=f"c{i}", run_id=run, strategy_id=victim.id, version=victim.version,
                                mint=f"m{i}", entry_ts=t, exit_ts=t + 1, pnl_sol=-0.05, pnl_pct=-30))
    rep = evolve_once(settings, store, lambda a, b: [e for e in events if a <= e.ts < b], now=events[-1].ts, start_sol=5.0)
    why = dict(rep.killed)[victim.key]
    assert why.startswith("losing: 4 trades, 0 won")


def test_generation_reports_its_phases_in_order(settings, events):
    settings.evolution.population = 2
    settings.evolution.window_h = 1.0
    settings.evolution.folds = 2
    settings.evolution.min_replay_trades = 1
    store = Store(settings.db_path)
    ensure_seeded(store, settings)
    seen = []
    evolve_once(settings, store, lambda a, b: [e for e in events if a <= e.ts < b], now=events[-1].ts,
                start_sol=0.5, phase_fn=seen.append)
    assert seen == ["judging", "learning", "learning from winners", "breeding", "screening", "ban scan", "whale scan", "done"]   # council off: breeding isn't held up


def test_a_full_swarm_of_winners_retires_its_weakest_to_keep_exploring(settings, events):
    settings.evolution.population = 4
    settings.evolution.window_h = 1.0
    settings.evolution.folds = 2
    settings.evolution.min_replay_trades = 1
    settings.evolution.min_live_trades = 2
    settings.evolution.min_free_slots = 1
    settings.evolution.seed = 3
    store = Store(settings.db_path)
    ensure_seeded(store, settings)
    fn = lambda a, b: [e for e in events if a <= e.ts < b]          # noqa: E731
    evolve_once(settings, store, fn, now=events[-1].ts, start_sol=5.0)
    swarm = [sp for sp, lbl in active_specs(store) if lbl == "swarm"]
    settings.evolution.population = len(swarm)                      # full: nobody would die or be born
    run = store.start_run("live")
    t = time.time() + 1
    for k, sp in enumerate(swarm):                                  # every agent wins, some more than others
        for i in range(3):
            store.insert_trade(dict(trade_id=f"w{k}{i}", run_id=run, strategy_id=sp.id, version=sp.version,
                                    mint=f"m{k}{i}", entry_ts=t, exit_ts=t + 1, pnl_sol=0.01 * (k + 1), pnl_pct=5.0 * (k + 1)))
    rep = evolve_once(settings, store, fn, now=events[-1].ts, start_sol=5.0)
    retired = store.evo_rows(event="retired")
    assert len(retired) == 1 and retired[0]["version"] == swarm[0].version          # the weakest winner, not a loser
    assert store.get_version(swarm[0].id, swarm[0].version)["status"] == "dead"
    assert store.get_version(swarm[-1].id, swarm[-1].version)["status"] == "swarm"  # the best is never touched
    assert dict(rep.killed)[swarm[0].key].startswith("retired to make room")
    assert all(r["event"] != "killed" for r in store.evo_rows(version=swarm[0].version))   # no penalty for its mutations


def test_the_whale_boost_bets_more_when_trusted_whales_are_in(settings, specs):
    from solmeme.sim.engine import Engine

    d = specs["holder_dip"].model_dump(mode="json")
    d["sizing"].update(boost_whales=1, boost_x=1.5)
    boosted = StrategySpec.model_validate(d)
    assert boosted.version != specs["holder_dip"].version                    # a real gene: a new version
    eng = Engine([(boosted, "champion")], settings, shared_wallet=True)
    r = eng.runners[0]

    class FV:
        def __init__(self, w):
            self.w = w

        def get(self, name):
            return self.w if name == "whale_buys" else None

    assert eng.boost(r, FV(0)) == 1.0 and eng.boost(r, FV(2)) == 1.5
    settings.portfolio.position_pct = 15
    settings.portfolio.max_position_sol = 100                                # (the cap still applies to boosted bets)
    assert abs(eng._size(r, 1.5) - 1.5 * eng._size(r)) < 1e-9
    plain = Engine([(specs["holder_dip"], "champion")], settings, shared_wallet=True)
    assert plain.boost(plain.runners[0], FV(5)) == 1.0                       # no gene, no boost


def test_the_breeder_adds_tunes_and_drops_the_boost(specs):
    base = specs["holder_dip"]
    seen = set()
    for seed in range(300):
        got = _breeder(seed).mutate(base)
        if got:
            seen |= {s for s in got[1] if s.startswith("boost:")}
    assert "boost:add" in seen
    d = base.model_dump(mode="json")
    d["sizing"].update(boost_whales=2, boost_x=1.5)
    with_boost = StrategySpec.model_validate(d)
    for seed in range(300):
        got = _breeder(seed).mutate(with_boost)
        if got:
            seen |= {s for s in got[1] if s.startswith("boost:")}
    assert {"boost:drop"} <= seen and any(s.startswith(("boost:x", "boost:whales")) for s in seen)


def test_a_lesson_fans_out_at_three_strengths(specs):
    from solmeme.lab.evolve import Lesson

    base = specs["dex_curve_momentum"]
    l = Lesson(feature="top10_pct", op=">", v=40.0, saved_sol=0.5, blocked=10, of=40)
    kids = _breeder(0).lesson_variants(base, l)
    cuts = sorted(next(c.v for c in k.entry.none if c.f == "top10_pct") for k, _ in kids)
    assert len(kids) == 3 and len({k.version for k, _ in kids}) == 3
    assert cuts[0] < 40.0 == cuts[1] < cuts[2]                       # milder, as learned, harsher
    assert all("lesson_fanout" in sigs and "lesson:top10_pct:>" in sigs for _, sigs in kids)


def test_trades_before_the_trust_cutoff_do_not_judge_agents(settings):
    import json as _json

    from solmeme.lab.evolve import Agent, _live_stats
    from solmeme.sim.wallet import TRUST_FILE, trusted_since

    assert trusted_since(settings) == 0.0
    (settings.data_dir / "session").mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "session" / TRUST_FILE).write_text(_json.dumps({"ts": 100.0}))
    assert trusted_since(settings) == 100.0

    class Store:
        def trades(self, sid, ver, run_mode=None):
            return [dict(entry_ts=50.0, pnl_sol=5.0, pnl_pct=500.0), dict(entry_ts=150.0, pnl_sol=-0.01, pnl_pct=-10.0)]

    class Spec:
        id, version = "s", "v"
    a = Agent.__new__(Agent)
    a.spec, a.born_ts = Spec(), 0.0
    from solmeme.lab.evolve import Fit
    a.fit = Fit()
    _live_stats(Store(), a, 0.5, 1, 0.0, trusted_since(settings))
    assert a.fit.live_n == 1 and a.fit.live_pnl_sol == -0.01          # the fake +500% before the cutoff is gone
