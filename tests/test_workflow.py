"""ResearchBot briefs, SniperBot setups and TraderBot permissions (ui/workflow.py)."""

from solmeme.core.events import CurveTrade, ExternalSignal, TokenLaunched
from solmeme.features.token_state import PUMP_K
from solmeme.sim.replay import run_replay
from solmeme.ui import workflow


def test_permissions_are_yours_to_set(settings):
    p = workflow.load_permissions(settings.data_dir)
    assert p["max_open"] == 4 and p["council_picks"] is True
    p = workflow.save_permissions(settings.data_dir, {"strategies": {"launch_sniper": False}, "max_bet_usd": 10, "council_picks": False})
    assert workflow.allowed(p, "launch_sniper@abc", 0.1, 100.0, 0, 0).startswith("launch_sniper is not allowed")
    assert "council picks are switched off" in workflow.allowed(p, "council_pick@x", 0.1, 100.0, 0, 0)
    assert workflow.allowed(p, "kol_follow@x", 0.1, 100.0, 4, 0).startswith("4 positions open")
    assert workflow.allowed(p, "kol_follow@x", 0.1, 100.0, 1, 0) is None
    assert workflow.cap_size(p, 0.5, 100.0) == 0.1                                   # $10 at $100/SOL
    assert workflow.load_permissions(settings.data_dir)["strategies"] == {"launch_sniper": False}   # saved


def test_research_brief_checks_wallets_holders_and_liquidity(settings, specs):
    mint = "Brief" + "1" * 35 + "pump"
    evs = [TokenLaunched(ts=0.0, source="pumpportal", mint=mint, creator="Dev", symbol="BRF", v_sol=30.5, v_tokens=PUMP_K / 30.5),
           CurveTrade(ts=5.0, source="pumpfun", mint=mint, trader="A", side="buy", sol=1.0, tokens=3e7, v_sol=31.5, v_tokens=PUMP_K / 31.5),
           ExternalSignal(ts=6.0, source="gmgn_trenches", mint=mint, signal="gmgn_new", data={"holders": 120, "top10_pct": 62.0})]
    eng = run_replay(settings, [(specs["kol_follow"], "champion")], evs)
    class S:
        engine = eng
    S.settings = settings
    b = workflow.brief(S, mint, 10.0)
    assert b["symbol"] == "BRF" and b["holders"]["count"] == 120
    assert b["liquidity"]["round_trip_pct"] is not None and b["liquidity"]["round_trip_pct"] > 0
    assert b["flag"] == "red" and any("top 10 hold 62%" in r for r in b["red"])
    assert "120 holders" in b["green"]


def test_unchecked_coin_is_not_green(settings, specs):
    mint = "Blind" + "1" * 35 + "pump"
    evs = [CurveTrade(ts=5.0, source="pumpfun", mint=mint, trader="A", side="buy", sol=1.0, tokens=3e7, v_sol=31.5, v_tokens=PUMP_K / 31.5)]
    eng = run_replay(settings, [(specs["kol_follow"], "champion")], evs)
    class S:
        engine = eng
    S.settings = settings
    b = workflow.brief(S, mint, 10.0)
    assert b["flag"] == "yellow" and any(y.startswith("creator unknown") for y in b["yellow"])
    assert b["holders"]["count"] is not None                    # GMGN had nothing: counted from the trade stream


def test_setups_show_near_misses_while_they_happen(settings):
    """The journal only gets a near-miss 30 min later (with its outcome); SniperBot must show it now."""
    from solmeme.strategy.spec import StrategySpec

    settings.prefilter.enabled = False
    spec = StrategySpec.model_validate(dict(id="waiter", entry=dict(all=[
        dict(id="buyers", f="trades_total", op=">=", v=50)]), sizing=dict(sol=0.1)))
    mint = "Near" + "1" * 36 + "pump"
    evs = [CurveTrade(ts=5.0, source="pumpfun", mint=mint, trader="A", side="buy", sol=1.0, tokens=3e7,
                      v_sol=31.5, v_tokens=PUMP_K / 31.5)]
    from solmeme.sim.engine import Engine

    eng = Engine([(spec, "champion")], settings, record_near_misses=True)
    for ev in evs:
        eng.step(ev)                                                  # live: no finish(), trackers still open
    assert not eng.journal.near_misses and eng.live_near_misses()    # not in the journal yet

    class S:
        engine = eng
    S.settings = settings
    got = workflow.setups(S)
    assert [g["strategy"] for g in got] == ["waiter"] and got[0]["waiting"][0]["rule"] == "buyers"
    assert workflow.research_feed(S)[0]["match"] == "nearly"
