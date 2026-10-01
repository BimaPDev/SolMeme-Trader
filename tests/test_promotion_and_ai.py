import time

import pytest

from solmeme.ai import reviewer
from solmeme.ai.llm import LLMResult
from solmeme.ai.promoter import Promoter, ensure_seeded
from solmeme.ai.reviewer import Windows, split_windows
from solmeme.ai.schemas import Finding, Proposal, ReviewOutput
from solmeme.core.store import Store
from solmeme.strategy.diff import Change


@pytest.fixture()
def store(settings):
    st = Store(settings.db_path)
    ensure_seeded(st, settings)
    return st


def test_invalid_and_duplicate_proposals(settings, store, specs):
    p = Promoter(settings, store)
    base = specs["curve_momentum"]
    bad = p.submit(base, [Change(op="remove_rule", rule_id="rug")], hypothesis="drop rug veto")
    assert bad["status"] == "invalid"
    ch = [Change(op="set_param", param="sl", value=20)]
    first = p.submit(base, ch, hypothesis="tighter stop")
    assert first["status"] == "pending"
    store.update_proposal(first["proposal_id"], status="rejected", decided_ts=time.time())
    again = p.submit(base, [Change(op="set_param", param="sl", value=22)], hypothesis="tighter stop again")
    assert again["status"] == "duplicate"
    retest = p.submit(base, [Change(op="set_param", param="sl", value=22)], hypothesis="new evidence", is_retest=True)
    assert retest["status"] == "pending"


def test_backtest_gate_rejects_starved_challenger(settings, store, specs, events):
    p = Promoter(settings, store)
    base = specs["curve_momentum"]
    row = p.submit(base, [Change(op="set_param", param="min_uniq", value=80)], hypothesis="ultra strict")
    out = p.backtest_gate(row, base, split_windows(events, 0.3).holdout)
    assert out["status"] == "backtest_failed"
    assert "holdout trades" in out["reason"]


def _live_trades(store, sid, version, pnls, t0):
    run = store.start_run("live")
    for i, pnl in enumerate(pnls):
        store.insert_trade(dict(trade_id=f"{sid}@{version}:{i}", run_id=run, strategy_id=sid, version=version,
                                mint=f"m{i}", entry_ts=t0 + i, exit_ts=t0 + i + 1, pnl_sol=pnl, pnl_pct=pnl * 1000,
                                size_sol=0.1, fees_sol=0.0, exit_reason="tp1", hold_s=1.0))


def test_shadow_gate_promotes_and_rollback(settings, store, specs):
    settings.promotion.min_shadow_trades = 20
    p = Promoter(settings, store)
    base = specs["curve_momentum"]
    row = p.submit(base, [Change(op="set_param", param="sl", value=25)], hypothesis="tighter stop")
    vch = row["challenger_version"]
    store.set_status(base.id, vch, "challenger")
    t0 = time.time() - 3600
    store.update_proposal(row["proposal_id"], status="shadow", shadow_started_ts=t0 - 1)
    _live_trades(store, base.id, vch, [0.05, 0.04, 0.06] * 10, t0)
    _live_trades(store, base.id, base.version, [-0.01, 0.0, -0.02] * 10, t0)
    decisions = p.shadow_gate()
    assert decisions and decisions[0]["verdict"] == "promote"
    assert store.champion(base.id)["version"] == vch
    assert p.rollback(base.id) == base.version
    assert store.champion(base.id)["version"] == base.version


def test_review_with_fake_llm(settings, store, events, monkeypatch):
    """End-to-end review cycle without calling the API: validation + holdout gate run for real."""
    out = ReviewOutput(
        summary="Losses cluster in tokens with an early whale buy.",
        findings=[Finding(category="filter_too_loose", description="whale dumps", evidence_trade_ids=[],
                          affected_trades=10, est_pnl_impact_sol=-0.5, confidence="medium")],
        proposals=[
            Proposal(title="veto whales", hypothesis="large early single buys precede dumps",
                     changes=[Change(op="add_rule", section="none", rule_id="whale", feature="max_buy_60s",
                                     operator=">", value=3.0)],
                     expected_effect="fewer stop-outs", expected_trades_change="fewer", evidence="AUC",
                     risk="blocks some runners", is_retest=False),
            Proposal(title="remove rug veto", hypothesis="x", changes=[Change(op="remove_rule", rule_id="rug")],
                     expected_effect="", expected_trades_change="more", evidence="", risk="", is_retest=False),
        ],
        feature_requests=["wallet funding-source clusters"],
    )
    calls = {}

    def fake(settings_, system, user, schema, *, purpose, store=None, effort=None):
        calls["system"], calls["user"] = system, user
        return LLMResult(out, "fake", "end_turn", cost_usd=0.0)

    monkeypatch.setattr(reviewer, "complete_json", fake)
    w = split_windows(events, 0.3)
    res = reviewer.review_strategy(settings, store, "curve_momentum", w, universe_rows=[])
    statuses = [p["status"] for p in res["proposals"]]
    assert statuses[1] == "invalid"                                   # locked rule
    assert statuses[0] in ("shadow", "backtest_failed", "backtest_passed")
    assert "Feature dictionary" in calls["system"] and "Near-misses" in calls["user"]
    assert store.query("SELECT COUNT(*) AS n FROM reviews")[0]["n"] == 1
