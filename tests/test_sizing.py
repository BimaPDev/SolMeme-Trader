"""Percentage bet sizing, the swarm's bet-size gene, and swarm wallets surviving a restart."""

import random
import time

from solmeme.ai.promoter import active_specs, ensure_seeded
from solmeme.core.store import Store
from solmeme.lab.evolve import SIZING_PCT_BOUNDS, Breeder, Credit
from solmeme.sim.engine import Engine
from solmeme.sim.live import LiveSession
from solmeme.strategy.spec import StrategySpec


def test_unset_pct_keeps_old_version_hashes(specs):
    s = specs["dex_curve_momentum"]
    assert s.sizing.pct is None and '"pct"' not in s.canonical_json()
    d = s.model_dump(mode="json")
    d["sizing"]["pct"] = 12.0
    assert StrategySpec.model_validate(d).version != s.version


def test_bets_are_a_share_of_the_wallet_and_shrink_with_it(settings, specs):
    settings.portfolio.position_pct = 20
    settings.portfolio.reserve_sol = 0.01
    settings.portfolio.starting_sol = 0.5
    eng = Engine([(specs["dex_curve_momentum"], "champion")], settings)
    r = eng.runners[0]
    assert abs(eng._size(r) - 0.1) < 1e-9                  # 20% of 0.5 SOL
    r.portfolio.account.cash = 0.1
    assert abs(eng._size(r) - 0.02) < 1e-9                 # wallet shrank → bet shrank, still trades
    r.portfolio.account.cash = 0.015
    assert eng._size(r) == 0.0                             # below the 0.01 SOL minimum after the reserve
    d = specs["dex_curve_momentum"].model_dump(mode="json")
    d["sizing"]["pct"] = 5
    eng2 = Engine([(StrategySpec.model_validate(d), "swarm")], settings)
    assert abs(eng2._size(eng2.runners[0]) - 0.025) < 1e-9  # the agent's own gene wins over the default


def test_fixed_sizing_without_position_pct(settings, specs):
    settings.portfolio.position_pct = None
    eng = Engine([(specs["dex_curve_momentum"], "champion")], settings)
    assert abs(eng._size(eng.runners[0]) - 0.1) < 1e-9


def test_bet_size_gene_mutates_within_bounds(specs):
    base = specs["near_graduation"]
    seen = set()
    for seed in range(200):
        br = Breeder(random.Random(seed), Credit([]), {}, {}, max_mutations=3, default_pct=15)
        got = br.mutate(base)
        if got and any(s.startswith("sizing_pct") for s in got[1]):
            pct = got[0].sizing.pct
            assert SIZING_PCT_BOUNDS[0] <= pct <= SIZING_PCT_BOUNDS[1]
            seen.add(pct)
    assert len(seen) > 5


def test_swarm_wallet_survives_a_restart(settings, specs):
    settings.portfolio.starting_sol = 0.5
    settings.portfolio.starting_usd = None
    store = Store(settings.db_path)
    ensure_seeded(store, settings)
    agent = specs["near_graduation"]
    d = agent.model_dump(mode="json")
    d["sizing"]["pct"] = 10
    agent = StrategySpec.model_validate(d)
    store.upsert_version(agent.id, agent.version, agent.canonical_json(), "swarm")
    run = store.start_run("live")
    t = time.time() + 1
    for i, pnl in enumerate((-0.2, 0.05)):
        store.insert_trade(dict(trade_id=f"t{i}", run_id=run, strategy_id=agent.id, version=agent.version,
                                mint=f"m{i}", entry_ts=t, exit_ts=t + 1, pnl_sol=pnl, pnl_pct=0))
    session = LiveSession(settings, store, active_specs(store), warmup_min=0, fresh=True)
    r = next(x for x in session.engine.runners if x.key == agent.key)
    champ = next(x for x in session.engine.runners if x.label == "champion")
    assert abs(r.portfolio.cash - 0.35) < 1e-9             # 0.5 start − 0.15 lost since birth
    assert abs(champ.portfolio.cash - 0.5) < 1e-9          # the shared wallet still starts fresh


def test_no_buys_until_live_prices_settle(settings, specs):
    settings.portfolio.entry_settle_s = 120
    session = LiveSession(settings, None, [(specs["dex_curve_momentum"], "champion")], warmup_min=0)
    r = session.engine.runners[0]
    assert not session.engine._can_enter(r, "M" * 43, time.time() + 60)
    assert session.engine._can_enter(r, "M" * 43, time.time() + 121)


def test_a_new_champion_gets_a_fresh_shared_wallet(settings, specs):
    settings.portfolio.starting_sol = 0.5
    settings.portfolio.starting_usd = None
    eng = Engine([(specs["dex_curve_momentum"], "champion"), (specs["near_graduation"], "swarm")], settings,
                 shared_wallet=True)
    old = eng.account
    old.cash = 0.05                                            # the shared wallet is nearly bust
    agent = eng.runners[1]
    changes = eng.sync_runners([(specs["dex_curve_momentum"], "champion"), (specs["near_graduation"], "champion")])
    assert eng.account is not old and eng.account.cash == 0.5 and eng.wallet_epoch == 1
    assert eng.in_shared_wallet(agent) and eng.in_shared_wallet(eng.runners[0])
    assert any("restarted" in c for c in changes)
    eng.sync_runners([(specs["dex_curve_momentum"], "champion"), (specs["near_graduation"], "champion")])
    assert eng.wallet_epoch == 1                               # no promotion, no reset


def test_a_demoted_champion_winds_down_then_leaves_the_shared_wallet(settings, specs):
    eng = Engine([(specs["dex_curve_momentum"], "champion"), (specs["near_graduation"], "champion")], settings,
                 shared_wallet=True)
    keep, gone = eng.runners
    mint = "D" * 43
    gone.pending_buys.add(mint)                                # still buying when it is demoted
    eng.sync_runners([(specs["dex_curve_momentum"], "champion"), (specs["near_graduation"], "trial")])
    assert eng.in_shared_wallet(gone)                          # finishes what the shared wallet paid for
    assert not eng._can_enter(gone, "E" * 43, time.time() + 1e6)
    gone.pending_buys.clear()
    changes = eng.sync_runners([(specs["dex_curve_momentum"], "champion"), (specs["near_graduation"], "trial")])
    assert not eng.in_shared_wallet(gone) and eng.in_shared_wallet(keep)
    assert gone.portfolio.cash == settings.portfolio.starting_sol
    assert any("left the champions' wallet" in c for c in changes)
    assert eng._can_enter(gone, "E" * 43, time.time() + 1e6)  # trades on as a trial, own wallet


def test_two_champions_never_stack_the_same_token(settings, specs):
    settings.portfolio.one_position_per_token = True
    eng = Engine([(specs["dex_curve_momentum"], "champion"), (specs["near_graduation"], "champion"),
                  (specs["migration_momentum"], "swarm")], settings, shared_wallet=True)
    a, b, agent = eng.runners
    mint = "T" * 43
    a.pending_buys.add(mint)
    assert not eng._can_enter(b, mint, 1.0)                 # the other champion is already buying it
    assert eng._can_enter(agent, mint, 1.0)                 # swarm agents trade their own wallets
    settings.portfolio.one_position_per_token = False
    assert eng._can_enter(b, mint, 1.0)


def test_champions_wallet_continues_after_a_restart(settings, specs):
    settings.portfolio.keep_wallet = True
    settings.portfolio.shared_wallet = True
    settings.portfolio.starting_usd = None
    settings.portfolio.starting_sol = 0.5
    s1 = LiveSession(settings, None, [(specs["dex_curve_momentum"], "champion")], warmup_min=0, fresh=True)
    s1.engine.account.cash = 0.9                             # it doubled
    s1._dump_state()
    s2 = LiveSession(settings, None, [(specs["dex_curve_momentum"], "champion")], warmup_min=0, fresh=True)
    assert abs(s2.engine.account.cash - 0.9) < 1e-9 and abs(s2.engine.account.starting_sol - 0.5) < 1e-9
    s3 = LiveSession(settings, None, [(specs["dex_curve_momentum"], "champion")], warmup_min=0, fresh=True, new_wallet=True)
    assert abs(s3.engine.account.cash - 0.5) < 1e-9         # ./start begins a new wallet


def test_wallet_splits_locked_profit_from_open_positions(settings, specs):
    from solmeme.sim.portfolio import Position
    from solmeme.sim.wallet import wallet_view
    settings.portfolio.starting_sol = 1.0
    settings.portfolio.starting_usd = None
    eng = Engine([(specs["dex_curve_momentum"], "champion")], settings, shared_wallet=True)
    eng.account.cash = 1.2 - 0.3                              # +0.2 locked in, 0.3 SOL now in a position
    r = eng.runners[0]
    r.portfolio.positions["M" * 43] = Position(trade_id="t", mint="M" * 43, symbol="M", entry_ts=0, entry_price=1e-6,
                                               tokens=1.0, initial_tokens=1.0, cost_sol=0.3)
    eng.held_sol = lambda x: 0.45 if x is r else 0.0          # worth 0.45 at market
    v = wallet_view(eng, 100.0, 100.0, None)
    assert abs(v["locked_sol"] - 0.2) < 1e-9 and abs(v["open_sol"] - 0.15) < 1e-9
    assert abs(v["balance_sol"] - (1.0 + v["locked_sol"] + v["open_sol"])) < 1e-9


def test_open_positions_survive_a_restart(settings, specs):
    from solmeme.core.events import TokenLaunched
    from solmeme.sim.portfolio import Position
    settings.portfolio.keep_wallet = True
    settings.portfolio.shared_wallet = True
    settings.portfolio.starting_usd = None
    settings.portfolio.starting_sol = 1.0
    spec = specs["dex_curve_momentum"]
    mint = "P" * 43

    def session():
        s = LiveSession(settings, None, [(spec, "champion")], warmup_min=0, fresh=True)
        s.engine.market.apply(TokenLaunched(ts=1.0, source="pumpportal", mint=mint, pool="pump", v_sol=30.0, v_tokens=1.073e9))
        return s

    s1 = session()
    r = s1.engine.runners[0]
    s1.engine.account.cash = 0.7                                   # 0.3 SOL went into the position
    r.portfolio.positions[mint] = Position(trade_id="keep-me", mint=mint, symbol="P", entry_ts=1.0, entry_price=2.8e-8,
                                           tokens=1e7, initial_tokens=1e7, cost_sol=0.3)
    before = s1.engine.shared_breakdown()
    s1._dump_state()
    s2 = session()
    s2._restore_positions(__import__("solmeme.sim.wallet", fromlist=["x"]).load_champions_wallet(settings))
    r2 = s2.engine.runners[0]
    after = s2.engine.shared_breakdown()
    assert mint in r2.portfolio.positions and r2.portfolio.positions[mint].trade_id == "keep-me"
    assert abs(after["cash"] - 0.7) < 1e-9                         # cash is cash, not a cash-out at market
    assert abs((after["cash"] + after["held"]) - (before["cash"] + before["held"])) < 1e-9


def test_reset_wallets_starts_money_over_but_keeps_the_record(settings, specs):
    from solmeme.core.store import Store
    from solmeme.lab.evolve import Agent, _live_stats
    from solmeme.sim.wallet import reset_wallets, wallet_reset_ts
    from solmeme.ui.swarm_view import build_swarm
    settings.portfolio.starting_sol = 0.5
    settings.portfolio.starting_usd = None
    store = Store(settings.db_path)
    agent = specs["near_graduation"]
    born = time.time() - 100
    store.upsert_version(agent.id, agent.version, agent.canonical_json(), "swarm")
    store.insert_evo(dict(ts=born, gen=1, strategy_id=agent.id, version=agent.version, parent_version=None, mate_version=None,
                          ops="[]", event="born", fitness=0, parent_fitness=0, reason=""))
    run = store.start_run("live")
    store.insert_trade(dict(trade_id="old", run_id=run, strategy_id=agent.id, version=agent.version, mint="m1",
                            entry_ts=born + 10, exit_ts=born + 20, pnl_sol=1.0, pnl_pct=200))
    reset = reset_wallets(settings)
    assert wallet_reset_ts(settings) == reset
    a = Agent(agent, "swarm", born)
    _live_stats(store, a, 0.5, 8, reset)
    assert a.fit.equity_frac == 1.0 and a.fit.live_n == 1 and a.fit.live_pnl_sol == 1.0   # fresh wallet, full record
    s = LiveSession(settings, store, [(agent, "swarm")], warmup_min=0, fresh=True)
    assert abs(s.engine.runners[0].portfolio.cash - 0.5) < 1e-9
    row = next(x for x in build_swarm(store, settings, None)["agents"] if x["key"] == agent.key)
    assert row["equity"] == 1.0 and row["pnl_sol"] == 1.0 and row["live_n"] == 1
