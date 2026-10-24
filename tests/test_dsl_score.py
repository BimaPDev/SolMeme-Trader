"""Confidence-score entries (strategy/dsl.py score terms) and score-sized bets."""

from pathlib import Path

from solmeme.strategy.dsl import evaluate, max_score, score
from solmeme.strategy.spec import StrategySpec, load_spec


class FV:
    def __init__(self, **v):
        self.v = v

    def get(self, k):
        return self.v.get(k)


SPEC = StrategySpec.model_validate(dict(
    id="s", entry=dict(all=[dict(id="px", f="secs_since_trusted_price", op="<", v=30)], min_score="$min",
                       score=[dict(id="kol", f="kol_buys_600s", op=">=", v=1, w=3),
                              dict(id="hold", f="gmgn_holders", op=">=", v=100, w=2),
                              dict(id="dump", f="dev_sold_pct", op=">=", v=40, w=-2)]),
    sizing=dict(score_x=2.0), params=dict(min=dict(value=3, lo=1, hi=5))))


def test_missing_data_adds_nothing_instead_of_blocking():
    assert evaluate(SPEC, FV(secs_since_trusted_price=5, kol_buys_600s=2))[0]            # holders unknown: still 3 points
    assert evaluate(SPEC, FV(secs_since_trusted_price=5, gmgn_holders=150, kol_buys_600s=0)) == (False, ["score"])


def test_red_flags_take_points_away_and_gates_still_block():
    assert score(SPEC, FV(kol_buys_600s=1, gmgn_holders=200, dev_sold_pct=60)) == 3
    assert not evaluate(SPEC, FV(secs_since_trusted_price=90, kol_buys_600s=5, gmgn_holders=500))[0]
    assert max_score(SPEC) == 5


def test_specs_without_a_score_keep_their_version():
    plain = StrategySpec.model_validate(dict(id="p", entry=dict(all=[dict(id="a", f="age_s", op="<", v=60)])))
    assert "score" not in plain.canonical_json() and "score_x" not in plain.canonical_json()


def test_score_sizes_the_bet():
    from solmeme.sim.engine import Engine

    class R:
        spec = SPEC
    assert Engine.boost(R, FV(kol_buys_600s=1)) == 1.0                  # at the buy line: normal size
    assert Engine.boost(R, FV(kol_buys_600s=1, gmgn_holders=300)) == 2.0  # top score: score_x


def test_score_trader_loads():
    s = load_spec(Path(__file__).parent.parent / "strategies" / "trials" / "score_trader.yaml")
    assert s.entry.score and s.sizing.score_x == 2.0 and max_score(s) > float(s.p(s.entry.min_score))


def test_feed_slots_go_to_champion_coins_before_swarm_coins():
    from solmeme.sources.watchlist import Watchlist

    w = Watchlist()
    w.set_pinned({"A_swarm", "Z_champ", "M_trial"}, {"A_swarm": 2, "Z_champ": 0, "M_trial": 1})
    assert w.priority_list(0)[:3] == ["Z_champ", "M_trial", "A_swarm"]
    w.set_pinned({"B", "A"})                                            # no ranks: alphabetical as before
    assert w.priority_list(0) == ["A", "B"]
