"""The council's track record (ai/track_record.py): picks are followed for 30 min, and every new pick's
prompt says how earlier ones moved and what a buy would cost and how it would exit."""

from types import SimpleNamespace as NS

from solmeme.ai import ask, track_record as tr


class Market:
    def __init__(self):
        self.px, self.sol_usd = {}, 110.0

    def get(self, mint):
        return NS(price_sol=self.px[mint], symbol=mint) if mint in self.px else None


def test_picks_are_followed_for_30_minutes_and_survive_a_restart(settings, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(tr.time, "time", lambda: now[0])
    m = Market(); m.px["A"] = 1.0
    f = tr.Follower(settings.data_dir)
    f.start(dict(id="p1", mint="A", symbol="AAA", verdict="avoid", action="passed"), 1.0, "early runner, 40 s old")
    now[0] += 300; m.px["A"] = 2.4; f.update(m)
    f2 = tr.Follower(settings.data_dir)                       # a restart: the open pick is still followed
    now[0] += 300; m.px["A"] = 0.6; f2.update(m)
    now[0] += 1300; m.px["A"] = 0.8
    assert f2.update(m) == 1 and not f2.open
    row = tr.load(settings.data_dir)[0]
    assert (row["max_x"], row["min_x"], row["end_x"], row["lane"], row["verdict"]) == (2.4, 0.6, 0.8, "early", "avoid")


def test_base_rates_split_by_verdict_and_lane(settings):
    for i in range(6):
        tr.record(settings.data_dir, dict(ts=i, mint=f"M{i}", verdict="avoid", lane="whales", max_x=2.2 if i < 3 else 1.1,
                                          min_x=0.5, end_x=0.6))
    for i in range(5):
        tr.record(settings.data_dir, dict(ts=10 + i, mint=f"N{i}", verdict="skipped", lane="early", max_x=1.6, min_x=0.9, end_x=1.2))
    text = tr.base_rates(settings.data_dir, lane="whales")
    assert "11 picks" in text and "the council said avoid: n=6: peaked 1.5x+ 50%, 2x+ 50%" in text
    assert "skipped by the global filter: n=5" in text and "this lane (whales): n=6" in text
    assert tr.base_rates(settings.data_dir.parent / "empty") == ""          # nothing yet: nothing said


def test_the_trade_plan_shows_size_cost_and_exits(settings, specs):
    from solmeme.strategy.spec import load_spec
    from pathlib import Path

    spec = load_spec(Path(__file__).parent.parent / "strategies" / "council_pick.yaml")
    settings.fills.priority_fee_sol, settings.fills.jito_tip_sol = 0.0002, 0.0
    settings.portfolio.position_pct, settings.portfolio.max_position_sol = 15, 1.0
    sess = NS(settings=settings, engine=NS(market=NS(sol_usd=110.0), runners=[NS(spec=spec, label="champion")]),
              dryrun=NS(equity=lambda: 0.3))
    text = tr.trade_plan(sess)
    assert "buys about 0.045 SOL ($4.95)" in text and "Exit plan: sell 50% at 2x" in text and "break even" in text


def test_a_pick_prompt_carries_the_track_record(settings, monkeypatch):
    for i in range(5):
        tr.record(settings.data_dir, dict(ts=i, mint=f"M{i}", verdict="wait", lane="whales", max_x=2.0, min_x=0.8, end_x=0.7))
    seen = []
    monkeypatch.setattr(ask, "evidence", lambda session, mt: ("evidence", {"card": {}, "strategies": []}))
    m = Market(); m.px["X"] = 1e-6
    class Eng:
        market = m; runners = []
        def _passes_prefilter(self, fv): return None
    monkeypatch.setattr(ask, "_passes_filter", lambda session, mint: (True, None))
    sess = NS(settings=settings, engine=Eng(), dryrun=None, pick_follower=tr.Follower(settings.data_dir))
    a = ask.pick(sess, "X", "3 whales from the book bought it in 10 min",
                 judge_fn=lambda s, text, **k: seen.append(text) or {"verdict": "avoid", "votes": []})
    assert "Track record" in seen[0] and "If you say buy" in seen[0]
    assert a["id"] in sess.pick_follower.open                                # and this pick is followed now
