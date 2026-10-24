"""Ask the council about one coin: address check, and votes weighted into one verdict."""

from solmeme.ai import ask
from solmeme.config import CouncilMember


def test_a_bad_address_is_refused(settings):
    class S:
        engine = object()
        asked_mints = {}
    S.settings = settings
    assert "error" in ask.start(S, "not-a-mint")
    assert "error" in ask.start(S, "0OIl" * 10)                 # base58 has no 0, O, I, l


def test_votes_become_one_verdict(settings, monkeypatch):
    settings.council.members = [CouncilMember(name=n, kind="openai", base_url="http://x", model="m") for n in ("a", "b", "c")]
    settings.council.cursor_pacing = False

    def fake(member, system, user):
        assert "buy" in user and "Coin X" in user
        if member.name == "c":
            raise RuntimeError("timeout")
        return {"verdict": "avoid" if member.name == "a" else "AVOID", "confidence": 0.8,
                "reasons": ["dev sold"], "risks": ["late"], "plan": ""}

    out = ask.judge(settings, "Coin X evidence", ask_fn=fake)
    assert out["verdict"] == "avoid" and out["agreement"] == 1.0
    assert [v.get("error", "")[:7] for v in out["votes"]] == ["", "", "timeout"]


def test_asks_are_listed_newest_first(settings):
    for i, mint in enumerate(("A" * 32, "B" * 32)):
        ask._save(settings, dict(id=f"id{i}", mint=mint, ts=i, status="done"))
    got = ask.list_asks(settings)["asks"]
    assert {a["id"] for a in got} == {"id0", "id1"}
    assert ask.list_asks(settings, "id1")["mint"] == "B" * 32


def test_picks_do_not_push_out_reviews(settings, monkeypatch):
    monkeypatch.setattr(ask, "KEEP", dict(pick=3, review=2, wallet=2, coin=2))
    ask._save(settings, dict(id="r0", kind="review", mint="R" * 32, ts=0))
    for i in range(6):
        ask._save(settings, dict(id=f"p{i}", kind="pick", mint="P" * 32, ts=i))
    got = {a["id"] for a in ask.list_asks(settings)["asks"]}
    assert "r0" in got and len([g for g in got if g.startswith("p")]) == 3


def test_wallet_votes_use_track_watch_avoid(settings):
    settings.council.members = [CouncilMember(name=n, kind="openai", base_url="http://x", model="m") for n in ("a", "b")]
    settings.council.cursor_pacing = False
    fake = lambda m, system, user: {"verdict": "track" if m.name == "a" else "watch", "confidence": 0.9 if m.name == "a" else 0.5}
    out = ask.judge(settings, "Wallet W", ask_fn=fake, system=ask.WALLET_SYSTEM, schema=ask.WALLET_SCHEMA,
                    verdicts=("track", "watch", "avoid"))
    assert out["verdict"] == "track" and set(out["score"]) == {"track", "watch", "avoid"}
    assert ask.judge(settings, "W", ask_fn=lambda m, s, u: {"verdict": "buy"})["verdict"] == "buy"   # coin asks unchanged


def test_tracked_wallets_are_capped_and_removable(settings):
    from solmeme.lab import my_wallets

    for i in range(my_wallets.MAX):
        assert my_wallets.add(settings.data_dir, f"W{i:03d}" + "1" * 36)["ok"]
    assert "error" in my_wallets.add(settings.data_dir, "X" * 40)
    my_wallets.remove(settings.data_dir, "W000" + "1" * 36)
    assert my_wallets.add(settings.data_dir, "X" * 40)["ok"] and len(my_wallets.load(settings.data_dir)) == my_wallets.MAX


def _session_for(settings, facts_seq):
    """A session stand-in whose wallet evidence comes from facts_seq, one item per check."""
    calls = iter(facts_seq)

    class S:
        pass
    S.settings = settings
    ask.wallet_evidence = lambda session, address: ("evidence", next(calls))
    return S


def test_a_tracked_wallet_is_rechecked_cheaply_and_removed_when_it_turns_bad(settings, monkeypatch):
    from solmeme.lab import my_wallets

    real = ask.wallet_evidence
    try:
        good = dict(whale={"status": "whale", "council": "trust", "win": 0.7, "round_trips": 10}, stats={}, ban=None, linked_banned=[])
        W = "Good" + "1" * 36
        my_wallets.add(settings.data_dir, W, verdict="track", by="council", fingerprint=ask.fingerprint(good))
        asked = []
        fake_judge = lambda *a, **k: asked.append(1) or {"verdict": "watch", "votes": []}
        s = _session_for(settings, [good, good, {**good, "whale": {**good["whale"], "win": 0.4}}, {**good, "whale": {**good["whale"], "win": 0.2}}])
        t0 = my_wallets.load(settings.data_dir)[W]["judged_ts"]
        assert ask.recheck(s, W, fake_judge, now=t0 + 4 * 3600)["result"] == "kept" and not asked     # nothing moved: free
        assert ask.recheck(s, W, fake_judge, now=t0 + 25 * 3600)["result"] == "kept" and len(asked) == 1   # daily re-judge: watch #1
        r = ask.recheck(s, W, fake_judge, now=t0 + 30 * 3600)                                             # win 70% -> 40%: watch #2
        assert r["result"] == "removed" and "Watch twice" in r["why"] and W not in my_wallets.load(settings.data_dir)
        assert my_wallets.removed(settings.data_dir)[-1]["by"] == "check"
        V = "Bann" + "2" * 36
        my_wallets.add(settings.data_dir, V, verdict="track", by="council")
        s = _session_for(settings, [{**good, "ban": {"role": "creator", "reason": "rug"}}])
        assert ask.recheck(s, V, fake_judge)["result"] == "removed" and len(asked) == 2      # banned: no judge needed
    finally:
        ask.wallet_evidence = real


def test_tracked_wallet_trades_are_logged_and_read_back_newest_first(settings):
    from solmeme.lab import my_wallets

    W, X = "W" * 40, "X" * 40
    for i, (w, side) in enumerate([(W, "buy"), (X, "buy"), (W, "sell")]):
        my_wallets.record_trade(settings.data_dir, dict(ts=float(i), wallet=w, mint=f"M{i}pump", side=side, price_sol=1e-7))
    got = my_wallets.trades_for(settings.data_dir, W)
    assert [t["side"] for t in got] == ["sell", "buy"] and all(t["wallet"] == W for t in got)


def test_why_keeps_the_winning_judges_reasons():
    a = {"verdict": "track", "votes": [{"judge": "a", "verdict": "track", "reasons": ["exits 2x on many coins", "not a dev"]},
                                        {"judge": "b", "verdict": "avoid", "reasons": ["too new"]}]}
    assert ask.why_of(a) == ["a: exits 2x on many coins", "a: not a dev"]


def test_a_clear_council_sell_sells_the_live_position(settings, monkeypatch):
    import threading

    class St:
        price_sol = 2e-7; symbol = "X"
    class Market:
        def get(self, m): return St()
    class Eng:
        market = Market()
    class S:
        engine = Eng()
    S.settings = settings
    monkeypatch.setattr(ask, "evidence", lambda session, mint: ("coin X evidence", {"card": {}, "strategies": []}))
    sold = []
    for verdicts, expect in ((["sell", "sell", "hold"], 1), (["sell", "hold", "hold"], 0)):
        it = iter(verdicts)
        monkeypatch.setattr(ask, "judge", lambda *a, **k: (lambda vs: {
            "verdict": max(set(vs), key=vs.count), "agreement": vs.count(max(set(vs), key=vs.count)) / 3,
            "votes": [{"judge": f"j{i}", "verdict": v, "reasons": ["dev dumping"]} for i, v in enumerate(vs)]})(verdicts))
        before = len(sold)
        ask.review_buy(S, {"key": "kol_follow@x", "mint": "M" * 40, "price": 1e-7, "why": ["kols 2"]},
                       lambda k, m, w: sold.append((k, m, w)))
        for t in threading.enumerate():
            if t.name.startswith("review-"):
                t.join(5)
        assert len(sold) - before == expect
    assert sold[0][0] == "kol_follow@x" and "dev dumping" in sold[0][2]


def test_the_whale_tape_finds_coins_several_whales_buy(settings):
    import time

    from solmeme.lab import my_wallets
    from solmeme.ui.launch import _whale_tape

    now = time.time()
    for i, w in enumerate(("W1", "W2", "W3", "W4")):
        my_wallets.record_whale_trade(settings.data_dir, dict(ts=now - 60 + i, wallet=w, mint="Hot" + "1" * 37, side="buy", price_sol=1e-7))
    my_wallets.record_whale_trade(settings.data_dir, dict(ts=now - 30, wallet="W1", mint="Cold" + "2" * 36, side="buy", price_sol=1e-7))

    class Book:
        wallets = {"W1": {"council": "trust", "score": 1.2}, "W2": {}, "W3": {}, "W4": {}}
    class S:
        engine = None; whalebook = Book(); _whale_set = {"W1", "W2", "W3", "W4"}
    S.settings = settings
    tape = _whale_tape(S)
    assert len(tape["trades"]) == 5 and tape["trades"][0]["mint"].startswith("Cold")           # newest first
    assert [(c["symbol"], c["whales"], c["trusted"]) for c in tape["clusters"]] == [("Hot111", 4, 1)]


def test_a_council_pick_buy_becomes_a_signal_and_a_skip_costs_nothing(settings, monkeypatch):
    from solmeme.core.events import ExternalSignal
    from solmeme.features.registry import FeatureView
    from solmeme.features.token_state import MarketState

    m = MarketState(); m.sol_usd = 100.0
    mint = "Pick" + "1" * 36 + "pump"
    m.apply(ExternalSignal(ts=1.0, source="gmgn_trenches", mint=mint, signal="x", data={"price_usd": 1e-5}))
    class Eng:
        market = m; now = 2.0
        def _passes_prefilter(self, fv): return None
    class S:
        engine = Eng()
    S.settings = settings
    monkeypatch.setattr(ask, "evidence", lambda session, mt: ("evidence", {"card": {}, "strategies": []}))
    got = []
    a = ask.pick(S, mint, "4 whales", judge_fn=lambda *x, **k: {"verdict": "buy", "agreement": 1.0,
                 "votes": [{"judge": "a", "verdict": "buy"}, {"judge": "b", "verdict": "buy"}]}, emit=got.append)
    assert a["action"] == "bought" and got and got[0].signal == "council_pick"
    m.apply(got[0])
    assert FeatureView(m.get(mint), got[0].ts + 5, m).get("council_pick") is True
    assert FeatureView(m.get(mint), got[0].ts + 700, m).get("council_pick") is False         # stale after 10 min
    Eng._passes_prefilter = lambda self, fv: "no_socials"
    calls = []
    a = ask.pick(S, mint, "4 whales", judge_fn=lambda *x, **k: calls.append(1) or {}, emit=got.append)
    assert a["action"] == "skipped" and not calls                                           # no Cursor call


def test_early_runners_need_two_signals_in_the_first_three_minutes():
    from solmeme.core.events import CurveTrade, ExternalSignal, TokenLaunched
    from solmeme.features.token_state import PUMP_K, MarketState

    m = MarketState(); m.whales = {"Whale1"}
    mint = "Sob" + "1" * 37 + "pump"
    m.apply(TokenLaunched(ts=0.0, source="pumpportal", mint=mint, v_sol=30.5, v_tokens=PUMP_K / 30.5))
    m.apply(CurveTrade(ts=26.0, source="pumpfun", mint=mint, trader="Whale1", side="buy", sol=1.0, tokens=3e7,
                       v_sol=31.5, v_tokens=PUMP_K / 31.5))
    st = m.get(mint)
    assert ask.early_signals(st, 30.0, m) == ["1 whale(s) from the book bought"]           # one signal: not yet
    m.apply(ExternalSignal(ts=50.0, source="gmgn_trenches", mint=mint, signal="gmgn_new", data={"holders": 53}))
    sig = ask.early_signals(st, 55.0, m)
    assert len(sig) == 2 and "53 holders already" in sig

    class Watch:
        def priority_list(self, now): return [mint]
    class Eng:
        market = m
    class S:
        engine = Eng(); watch = Watch()
    assert [c[0] for c in ask.early_candidates(S, 55.0)] == [mint]
    assert ask.early_candidates(S, 400.0) == []                                              # too old: not early


def test_wallet_forensics_flags_its_own_dead_launches(settings):
    from solmeme.core.events import TokenLaunched, WalletTrade
    from solmeme.features.token_state import PUMP_K, MarketState

    m = MarketState(); m.sol_usd = 100.0
    W = "Insider" + "1" * 33
    own, good = "Own" + "1" * 37 + "pump", "Good" + "2" * 36 + "pump"
    m.apply(TokenLaunched(ts=0.0, source="pumpportal", mint=own, creator=W, symbol="OWN", v_sol=30.5, v_tokens=PUMP_K / 30.5))
    m.apply(TokenLaunched(ts=0.0, source="pumpportal", mint=good, creator="Other", symbol="GOOD", v_sol=30.5, v_tokens=PUMP_K / 30.5))
    for mint in (own, good):
        m.apply(WalletTrade(ts=10.0, source="gmgn_tracked", wallet=W, mint=mint, side="buy", sol=1.0))
    m.get(own).set_price(20.0, 5e-7); m.get(own).set_price(30.0, 2e-8)          # pumped, then dead
    m.get(good).set_price(20.0, 9e-8)
    class Eng:
        market = m; now = 40.0
    class S:
        engine = Eng()
    S.settings = settings
    out = ask.coin_forensics(S, W, ask._sightings(S, W))
    assert out["own"] == 1 and out["dead"] == 1 and len(out["rows"]) == 2
    row = next(r for r in out["rows"] if r["symbol"] == "OWN")
    assert "it created this coin itself" in row["line"] and "(dead)" in row["line"]
    assert "1 it created itself" in out["summary"]
