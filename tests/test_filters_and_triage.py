from solmeme.ai.triage import triage
from solmeme.config import TriageCfg
from solmeme.lab.metrics import compute
from solmeme.sim.replay import run_replay


def test_prefilter_blocks_everything_when_rule_matches_all(settings, specs, events):
    settings.prefilter.none = [{"id": "all", "f": "mcap_sol", "op": ">", "v": 0}]
    eng = run_replay(settings, [(specs["curve_momentum"], "champion")], events)
    assert eng.journal.trades == []
    assert eng.prefilter_blocked.get("all", 0) > 0


def test_prefilter_off_allows_trades(settings, specs, events):
    settings.prefilter.enabled = False
    eng = run_replay(settings, [(specs["curve_momentum"], "champion")], events)
    assert eng.journal.trades


def _trade(pnl, reason="tp1", after=1.0):
    return dict(trade_id=f"t{pnl}", strategy_id="s", version="v", pnl_sol=pnl, pnl_pct=pnl * 1000,
                exit_ts=1, hold_s=10, fees_sol=0.001, exit_reason=reason, mfe_x=1.2, peak_after_exit_x=after,
                entry_features={})


def test_triage_skips_small_samples():
    trades = [_trade(0.01)] * 5
    assert not triage(compute(trades), trades, [], TriageCfg()).call_ai


def test_triage_skips_healthy_strategy_without_money_flags():
    trades = [_trade(0.05 + i * 1e-4) for i in range(30)]
    t = triage(compute(trades), trades, [], TriageCfg())
    assert not t.call_ai and "healthy" in t.reason


def test_triage_calls_ai_on_losing_stop_outs():
    trades = [_trade(-0.04 - i * 1e-4, "stop_loss") for i in range(12)] + [_trade(0.02) for _ in range(10)]
    t = triage(compute(trades), trades, [], TriageCfg())
    assert t.call_ai and any("stop_loss" in f for f in t.flags)
