import json
import time
import urllib.request

import pytest

from solmeme.features.token_state import TokenState
from solmeme.sim.live import LiveSession
from solmeme.ui.server import Hub, serve
from solmeme.ui.snapshot import build_snapshot

MINT = "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"


def test_snapshot_lists_strategies_and_empty_book(settings, specs):
    session = LiveSession(
        settings, None, [(specs["dex_curve_momentum"], "champion")],
        warmup_min=0, ai=False,
    )
    session.engine.market.sol_usd = 150
    snap = build_snapshot(session)
    assert snap["paper"] is True
    assert snap["sol_usd"] == 150
    assert snap["positions"] == []
    assert snap["boards"] is None
    row = snap["strategies"][0]
    assert row["role"] == "champion"
    assert row["balance_usd"] == 150 * row["balance_sol"]


def test_localhost_serves_snapshot_and_page(settings, specs, tmp_path):
    session = LiveSession(
        settings, None, [(specs["dex_curve_momentum"], "champion")],
        warmup_min=0, ai=False,
    )
    hub = Hub()
    hub.update(build_snapshot(session))
    (tmp_path / "index.html").write_text("<!doctype html><title>solmeme</title>", encoding="utf-8")
    httpd, port = serve(hub, tmp_path, port=0)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/snapshot") as resp:
            data = json.load(resp)
        assert data["paper"] is True
        assert data["strategies"][0]["key"]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
            assert b"solmeme" in resp.read()
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/../../etc/passwd")
        except urllib.error.HTTPError as exc:
            assert exc.code in (403, 404)
        else:
            raise AssertionError("path outside the site root was served")
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_wallet_tracks_best_strategy_against_goal(settings, specs):
    settings.portfolio.goal_usd = 100.0
    session = LiveSession(
        settings, None,
        [(specs["dex_curve_momentum"], "champion"), (specs["near_graduation"], "champion")],
        warmup_min=0, ai=False,
    )
    session.engine.market.sol_usd = 100
    session.engine.runners[1].portfolio.cash += 1.5
    wallet = build_snapshot(session)["wallet"]
    start = settings.portfolio.starting_sol
    assert wallet["strategies"] == 2
    assert wallet["best"] == session.engine.runners[1].key
    assert wallet["start_sol"] == start
    assert wallet["start_usd"] == start * 100
    assert wallet["pnl_sol"] == 1.5
    assert wallet["pnl_usd"] == 150
    assert wallet["goal_usd"] == 100
    assert wallet["reached"] == [r.key for r in session.engine.runners]


def test_shared_wallet_is_one_pool_for_every_strategy(settings, specs, monkeypatch):
    monkeypatch.setattr("solmeme.sim.live.fetch_sol_usd", lambda: 125.0)
    settings.portfolio.starting_usd = 50.0
    settings.portfolio.goal_usd = 100.0
    settings.portfolio.shared_wallet = True
    session = LiveSession(
        settings, None,
        [(specs["dex_curve_momentum"], "champion"), (specs["near_graduation"], "champion")],
        warmup_min=0, ai=False,
    )
    a, b = session.engine.runners
    assert a.portfolio.account is b.portfolio.account
    a.portfolio.cash -= 0.1                      # one strategy spends ...
    assert b.portfolio.cash == pytest.approx(0.3)  # ... and the other sees less cash

    session.engine.market.sol_usd = 125.0
    snap = build_snapshot(session)
    wallet = snap["wallet"]
    assert snap["shared_wallet"] is True
    assert wallet["shared"] is True and wallet["best"] is None
    assert wallet["start_usd"] == pytest.approx(50.0)
    assert wallet["balance_sol"] == pytest.approx(0.3)
    assert wallet["pnl_usd"] == pytest.approx(-12.5)
    assert wallet["reached"] == []
    row = snap["strategies"][0]
    assert row["cash_sol"] is None
    assert row["balance_sol"] == 0                 # no open positions, so nothing held

    b.portfolio.cash += 0.6
    session.wallet_history.observe(session.engine, 125.0, 1.0)
    wallet = session.wallet()
    assert wallet["reached"] == ["wallet"]
    assert wallet["range"]["high"] == pytest.approx(112.5)


def test_boards_fall_back_to_pump_and_live_trending(settings, specs):
    session = LiveSession(settings, None, [(specs["dex_curve_momentum"], "champion")], warmup_min=0, ai=False)
    now = time.time()
    for i, vol, age in ((0, 500.0, 30), (1, 9_000.0, 30), (2, None, 30), (3, 99_000.0, 3_600)):
        st = TokenState(mint=f"{MINT[:-1]}{i}", first_seen_ts=now, symbol=f"T{i}")
        st.dex = {"mcap_usd": 1000.0 * (i + 1), **({"volume_h1_usd": vol} if vol else {})}
        st.dex_ts = now - age                       # T3 has the most volume but its data is an hour old
        session.engine.market.tokens[st.mint] = st
    session.boards_snap = {
        "trenches": {}, "trending": [], "cards": [],
        "pump": {"new": [{"symbol": "NEW", "mint": MINT, "mcap_usd": 4000, "bonding_pct": 2.0}],
                 "almost": [], "graduated": []},
    }
    boards = build_snapshot(session)["boards"]
    assert boards["trenches_source"] == "pump.fun"
    assert boards["trending_source"] == "dexscreener"
    assert [r["symbol"] for r in boards["trending"]] == ["T1", "T0"]
    assert boards["trending"][0]["volume_usd"] == 9_000.0


def test_challengers_trade_their_own_copy_of_the_shared_wallet(settings, specs, monkeypatch):
    monkeypatch.setattr("solmeme.sim.live.fetch_sol_usd", lambda: 125.0)
    settings.portfolio.starting_usd = 50.0
    settings.portfolio.shared_wallet = True
    champ = specs["migration_momentum"]
    trial = champ.with_params({"sl": 15})
    session = LiveSession(settings, None, [(champ, "champion"), (trial, "challenger")], warmup_min=0, ai=False)
    eng = session.engine
    a, b = eng.runners
    assert eng.in_shared_wallet(a) and not eng.in_shared_wallet(b)
    b.portfolio.cash -= 0.3                          # the challenger spends its own copy …
    assert eng.account.cash == pytest.approx(0.4)    # … and the real wallet does not move
    eng.market.sol_usd = 125.0
    snap = build_snapshot(session)
    assert snap["wallet"]["balance_sol"] == pytest.approx(0.4)
    assert [r["in_wallet"] for r in snap["strategies"]] == [True, False]

    # promoted mid-run while holding nothing: it moves onto the shared wallet
    eng.sync_runners([(trial, "champion")])
    assert eng.in_shared_wallet(b) and b.label == "champion"


def test_history_and_swarm_endpoints(settings, specs, tmp_path):
    session = LiveSession(settings, None, [(specs["dex_curve_momentum"], "champion")], warmup_min=0, ai=False)
    session.engine.market.sol_usd = 150
    session.series.observe(session, time.time())
    hub = Hub()
    hub.update(build_snapshot(session))
    hub.history_fn = session.series.as_dict
    hub.swarm_fn = lambda: {"generation": 4, "agents": []}
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    httpd, port = serve(hub, tmp_path, port=0)
    try:
        def get(path):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                return json.loads(r.read())
        hist = get("/api/history")
        assert len(hist["wallet"]) == 1 and "dex_curve_momentum@" in next(iter(hist["runners"]))
        assert get("/api/swarm")["generation"] == 4
        assert get("/api/snapshot")["swarm_clock"]["every_min"] == settings.evolution.every_min
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_best_performing_and_agent_profile(settings, specs):
    from solmeme.core.store import Store
    from solmeme.ui.swarm_view import agent_detail, build_best

    settings.portfolio.starting_usd = None
    settings.portfolio.starting_sol = 1.0
    store = Store(settings.db_path)
    parent, child = specs["near_graduation"], specs["near_graduation"].with_params({"sl": 20})
    store.upsert_version(parent.id, parent.version, parent.canonical_json(), "trial")
    store.upsert_version(child.id, child.version, child.canonical_json(), "swarm", parent_version=parent.version)
    run = store.start_run("live")
    for i, (sym, pnl) in enumerate([("AAA", 0.5), ("BBB", -0.1), ("AAA", 0.3), ("CCC", 0.1), ("DDD", -0.05)]):
        store.insert_trade(dict(trade_id=f"t{i}", run_id=run, strategy_id=child.id, version=child.version, mint=sym * 14,
                                symbol=sym, entry_ts=time.time() + i, exit_ts=time.time() + i + 1, pnl_sol=pnl, pnl_pct=pnl * 100))
    best = build_best(store, settings, None)
    assert best[0]["key"] == child.key and abs(best[0]["equity"] - 1.75) < 1e-9 and best[0]["top_token"]["symbol"] == "AAA"
    d = agent_detail(store, settings, child.key, None)
    assert [n["version"] for n in d["lineage"]] == [child.version, parent.version]
    assert any("stop_loss_pct" in c for c in d["changes_vs_parent"])
    assert d["tokens"][0]["symbol"] == "AAA" and round(d["tokens"][0]["share_of_profit"], 2) == round(0.8 / 0.9, 2)
    assert d["spec"]["rules"] and len(d["trades"]) == 5


def test_positions_carry_invested_and_worth_now(settings, specs):
    from solmeme.sim.portfolio import Position
    session = LiveSession(settings, None, [(specs["dex_curve_momentum"], "champion")], warmup_min=0, ai=False)
    session.engine.market.sol_usd = 100.0
    r = session.engine.runners[0]
    r.portfolio.positions["M" * 43] = Position(trade_id="t", mint="M" * 43, symbol="M", entry_ts=1.0, entry_price=1e-6,
                                               tokens=0.0, initial_tokens=1.0, cost_sol=0.15, proceeds_sol=0.2)
    row = build_snapshot(session)["positions"][0]
    assert row["invested_usd"] == 15.0 and abs(row["worth_usd"] - 20.0) < 1e-9 and row["mint"] == "M" * 43
