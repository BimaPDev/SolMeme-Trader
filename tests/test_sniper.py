from solmeme.sniper.analyze import analyze
from solmeme.sniper.learn import SniperLearner


def test_analyze_vetoes_rug():
    card = analyze(
        {"hard_rug": True, "unique_buyers_30s": 20, "buys_30s": 20},
        mint="M", ts=1, price=1.0,
    )
    assert "hard_rug" in card.vetoes
    assert "no_flow" not in card.vetoes


def test_analyze_healthy_vector():
    card = analyze(
        {"unique_buyers_30s": 14, "buy_sell_ratio_30s": 2.5, "net_flow_30s": 4, "buys_30s": 14},
        mint="M", ts=1, price=1.0,
    )
    assert card.vetoes == []
    assert card.vector["unique_buyers_30s"] == 14
    assert "flow" in card.parts


def test_learner_separates_buyers():
    learner = SniperLearner()
    for _ in range(80):
        learner.observe({"unique_buyers_30s": 24.0}, True, action="skip")
        learner.observe({"unique_buyers_30s": 1.0}, False, action="skip")
    assert learner.score({"unique_buyers_30s": 24}) > learner.score({"unique_buyers_30s": 1})


def test_threshold_rises_when_buys_lose():
    learner = SniperLearner()
    start = learner.threshold()
    for _i in range(12):
        learner.observe({"unique_buyers_30s": 10}, False, action="buy", pnl=-0.05)
    assert learner.threshold() > start


def test_replay_learns_on_synth(settings, events):
    from solmeme.sniper.session import run_sniper

    eng, sniper = run_sniper(settings, events, store=None, decision_s=12, horizon_s=600)
    rep = sniper.report()
    assert rep["decisions"] > 0
    assert rep["summary"]["labels"] > 0
    assert rep["buys"] > 0
    prefilter_skips = sum(1 for tr in sniper.trackers.values() if any(
        v in tr.vetoes for v in ("has_price", "rug", "gmgn_rug", "dev_dumped", "whale_top1", "crashed")))
    assert prefilter_skips < rep["decisions"]
    again = SniperLearner.from_dict(sniper.learner.to_dict())
    assert again.summary()["labels"] == sniper.learner.summary()["labels"]
    assert eng.events > 0


def test_resume_does_not_retrain(settings, events):
    from solmeme.core.store import Store
    from solmeme.sim.engine import Engine, Journal
    from solmeme.sniper.model import STRATEGY_ID
    from solmeme.sniper.session import Sniper, run_sniper
    from solmeme.strategy.spec import load_dir

    store = Store(settings.db_path)
    _, first = run_sniper(settings, events, store=store, decision_s=12, horizon_s=600)
    learned = first.learner.n_labels
    assert learned > 0
    spec = next(s for s in load_dir(settings.strategies_dir) if s.id == STRATEGY_ID)
    eng = Engine([(spec, "champion")], settings, journal=Journal())
    again = Sniper(eng, store, decision_s=12, horizon_s=600, resume=True)
    again.attach()
    for ev in events:
        eng.step(ev)
    again.flush(eng.now, force=True)
    assert again.learner.n_labels == learned


def test_learner_persists_in_store(settings):
    from solmeme.core.store import Store

    store = Store(settings.db_path)
    store.save_sniper({
        "features": ["unique_buyers_30s"],
        "weights": {"unique_buyers_30s": 0.5},
        "bias": -0.4,
        "n_labels": 3,
    })
    assert store.load_sniper()["n_labels"] == 3
