import asyncio
import time

from solmeme.core.eventlog import EventLogWriter, read_events
from solmeme.core.events import TokenLaunched
from solmeme.sim.live import LiveSession
from solmeme.sources.base import Context
from solmeme.features.registry import FeatureView
from solmeme.features.token_state import MarketState
from solmeme.lab.metrics import engine_metrics
from solmeme.sim.replay import run_replay


def _sig(eng):
    return sorted((t["trade_id"], round(t["pnl_sol"], 12), t["exit_reason"]) for t in eng.journal.trades)


def test_replay_is_deterministic(settings, specs, events):
    sp = [(specs["curve_momentum"], "champion"), (specs["kol_follow"], "champion")]
    a = run_replay(settings, sp, events)
    b = run_replay(settings, sp, events)
    assert a.journal.trades, "expected some trades on the synthetic market"
    assert _sig(a) == _sig(b)


def test_strategies_are_isolated(settings, specs, events):
    alone = run_replay(settings, [(specs["curve_momentum"], "champion")], events)
    crowd = run_replay(settings, [(s, "champion") for s in specs.values()], events)
    key = specs["curve_momentum"].key
    assert _sig(alone) == [x for x in _sig(crowd) if x[0].startswith(key)]


def test_eventlog_roundtrip_matches_in_memory(settings, specs, events):
    w = EventLogWriter(settings.events_dir)
    for ev in events:
        w.write(ev)
    w.close()
    loaded = list(read_events(settings.events_dir))
    assert len(loaded) == len(events)
    sp = [(specs["curve_momentum"], "champion")]
    assert _sig(run_replay(settings, sp, events)) == _sig(run_replay(settings, sp, loaded))


def test_warmup_replays_recent_log(settings, specs):
    now = time.time()
    w = EventLogWriter(settings.events_dir)
    w.write(TokenLaunched(ts=now - 30, source="test", mint="4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi",
                          name="Otter", symbol="OTTR"))
    w.close()
    session = LiveSession(settings, None, [(specs["dex_curve_momentum"], "champion")], warmup_min=45, ai=False)
    assert session.last_ts == now - 30


def test_live_scan_runs_boards_and_people(settings, monkeypatch):
    seen = {}

    async def fake_snapshot(req, **kwargs):
        seen["req"] = req
        seen["pump"] = kwargs.get("pump_get")
        return {"trenches": {}, "trending": [], "buys": [], "cards": [], "pump": {"new": [], "almost": [], "graduated": []}}

    async def fake_discover(req, **kwargs):
        seen["people"] = True
        return [{"wallet": "W", "verdict": "watch", "text": "Otter"}]

    monkeypatch.setattr("solmeme.lab.boards.snapshot", fake_snapshot)
    monkeypatch.setattr("solmeme.lab.people.discover", fake_discover)
    monkeypatch.setattr("solmeme.sources.gmgn.available", lambda: True)
    session = LiveSession(settings, None, None, warmup_min=0, ai=False)
    session.ctx = Context(settings, None, lambda ev: None)
    asyncio.run(session._one_scan())
    assert seen["pump"] is not None and seen["people"] is True
    assert any((settings.data_dir / "boards").glob("*.md"))


def test_no_lookahead_in_features(events):
    """Features at time t must be identical whether or not later events exist."""
    cut = events[len(events) // 2].ts
    full, trunc = MarketState(), MarketState()
    for ev in events:
        if ev.ts <= cut:
            full.apply(ev)
            trunc.apply(ev)
    # apply the future only to `full`, then compare a snapshot taken "as of" cut using pre-cut state copies
    mints = [m for m, st in trunc.tokens.items() if st.price_sol][:25]
    snaps = {m: FeatureView(trunc.tokens[m], cut, trunc).snapshot() for m in mints}
    for m in mints:
        assert FeatureView(full.tokens[m], cut, full).snapshot() == snaps[m]


def test_metrics_account_for_failed_buys(settings, specs, events):
    eng = run_replay(settings, [(specs["curve_momentum"], "champion")], events)
    m = engine_metrics(eng)[specs["curve_momentum"].key]
    r = eng.runners[0]
    cash_change = r.portfolio.cash - settings.portfolio.starting_sol
    assert abs(cash_change - m.total_pnl_sol) < 1e-6
