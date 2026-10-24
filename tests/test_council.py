"""Council: judges' rules are checked against the trades before any vote, approved ideas are bred
into swarm children, and ideas (and their judges) are scored by what the carriers did live."""

import json
import random
import time

import numpy as np

from solmeme.ai import council
from solmeme.ai.council import Rule, helpful, in_sample, judge_ideas, parse_rules, reputations, sit
from solmeme.config import CouncilMember
from solmeme.core.store import Store
from solmeme.lab.evolve import Breeder, Credit


def _trades(n=40, seed=0):
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        holders = float(rng.uniform(5, 200))
        out.append({"mint": f"m{i}", "symbol": f"C{i}", "pnl_sol": 0.03 if holders > 80 else -0.02,
                    "pnl_pct": 30 if holders > 80 else -20, "mfe_x": 1.5, "exit_reason": "tp",
                    "entry_features": {"gmgn_holders": holders, "top10_pct": float(rng.uniform(10, 60))}})
    return out


def test_rules_are_validated_and_checked_on_the_trades():
    trades = _trades()
    ranges = {"gmgn_holders": (5, 200), "top10_pct": (10, 60)}
    rules = parse_rules({"rules": [
        {"feature": "gmgn_holders", "op": ">=", "value": 80, "kind": "require", "reason": "organic"},
        {"feature": "not_a_feature", "op": ">=", "value": 1},
        {"feature": "gmgn_holders", "op": "==", "value": 1},
    ]}, ranges, limit=3)
    assert [r.text() for r in rules] == ["buy only when gmgn_holders >= 80"]
    ok, _ = helpful(in_sample(rules[0], trades))
    assert ok
    bad = in_sample(Rule("gmgn_holders", "<=", 80, "require"), trades)          # keeps the losers
    assert not helpful(bad)[0] and bad["saved_sol"] < 0


def _sit(settings, store, specs, answers):
    calls = []

    def fake_ask(member, system, user):
        calls.append(member.name)
        return answers(member.name, user)

    settings.council.enabled = True
    settings.council.min_trades = 10
    settings.council.members = [CouncilMember(name="a", kind="openai", base_url="http://x", model="m"),
                                CouncilMember(name="b", kind="openai", base_url="http://x", model="m")]
    orig = council.ask
    council.ask = fake_ask
    try:
        spec = specs["near_graduation"]
        return sit(settings, store, 1, {spec.id: spec}, {spec.id: _trades()}), calls
    finally:
        council.ask = orig


def test_sitting_approves_rules_that_pass_the_data_and_the_vote(settings, specs):
    store = Store(settings.db_path)

    def answers(name, user):
        if "Vote on each" in user:
            return {"votes": [{"n": 1, "approve": True, "confidence": 0.9, "reason": "makes sense"}]}
        if name == "a":
            return {"rules": [{"feature": "gmgn_holders", "op": ">=", "value": 80, "kind": "require", "reason": "organic"}]}
        return {"rules": [{"feature": "top10_pct", "op": ">=", "value": 999, "kind": "avoid"}]}   # blocks nothing

    rows, calls = _sit(settings, store, specs, answers)
    by_status = {r["status"]: r for r in rows}
    assert by_status["testing"]["feature"] == "gmgn_holders" and by_status["testing"]["approved"] == 1
    assert by_status["unhelpful"]["feature"] == "top10_pct"
    assert calls.count("a") == 2 and calls.count("b") == 2               # explain + vote each
    assert json.loads(by_status["testing"]["votes_json"])["b"]["approve"] is True


def test_a_sitting_is_written_as_a_chat_transcript(settings, specs):
    store = Store(settings.db_path)

    def answers(name, user):
        if "Vote on each" in user:
            return {"votes": [{"n": 1, "approve": True, "confidence": 0.9, "reason": "real demand"}]}
        if name == "a":
            return {"why": "winners had many holders",
                    "rules": [{"feature": "gmgn_holders", "op": ">=", "value": 80, "kind": "require", "reason": "organic"}]}
        return {"why": "nothing clear", "rules": [{"feature": "top10_pct", "op": ">=", "value": 999, "kind": "avoid"}]}

    _sit(settings, store, specs, answers)
    sittings = store.council_sittings()
    assert len(sittings) == 1 and sittings[0]["gen"] == 1 and sittings[0]["closed"] == 1
    msgs = store.council_messages(sittings[0]["sitting"])
    phases = [m["phase"] for m in msgs]
    assert phases[0] == "open" and phases[-1] == "close"
    assert phases.index("explain") < phases.index("check") < phases.index("vote") < phases.index("verdict")
    said = {(m["speaker"], m["kind"], m["phase"]) for m in msgs}
    assert {("a", "asking", "explain"), ("a", "answer", "explain"), ("b", "answer", "vote")} <= said
    a_said = next(m["text"] for m in msgs if m["speaker"] == "a" and m["kind"] == "answer" and m["phase"] == "explain")
    assert "winners had many holders" in a_said and "buy only when gmgn_holders >= 80" in a_said
    checks = [m["text"] for m in msgs if m["phase"] == "check"]
    assert any(t.startswith("✓ a's") for t in checks) and any(t.startswith("✗ b's") for t in checks)
    verdict = next(m for m in msgs if m["phase"] == "verdict")
    assert verdict["text"].startswith("APPROVED") and json.loads(verdict["data_json"])["approved"] is True
    evidence = next(m for m in msgs if m["phase"] == "explain" and m["speaker"] == "council")
    assert "gmgn_holders" in json.loads(evidence["data_json"])["evidence"]      # what the judges were shown


def test_remote_judges_answer_in_parallel_and_local_ones_take_turns(settings, monkeypatch):
    import threading

    store = Store(settings.db_path)
    live, peak_local, lock = set(), [0], threading.Lock()

    def fake_ask(member, system, user):
        with lock:
            live.add(member.name)
            peak_local[0] = max(peak_local[0], sum(n.startswith("gpu") for n in live))
        time.sleep(0.2)
        with lock:
            live.discard(member.name)
        return {"ok": member.name}

    monkeypatch.setattr(council, "ask", fake_ask)
    members = [CouncilMember(name=n, kind=k, base_url="http://x", model="m")
               for n, k in [("api1", "openai"), ("api2", "openai"), ("api3", "openai"), ("gpu1", "ollama"), ("gpu2", "ollama")]]
    t0 = time.time()
    out = council._ask_all(store, members, [("q", "explain x", "text")], lambda _: None)
    assert time.time() - t0 < 0.6                   # 3 API judges overlap; the 2 GPU judges take 0.4s in turn
    assert peak_local[0] == 1                       # never two local models loaded at once
    assert all(out[m.name]["q"] == {"ok": m.name} for m in members)
    logged = store.conn.execute("select count(*) from council_calls").fetchone()[0]
    assert logged == len(members)


def test_backup_judges_only_answer_what_the_others_could_not(settings, monkeypatch):
    store = Store(settings.db_path)
    asked = []

    def fake_ask(member, system, user):
        asked.append((member.name, user))
        if member.name == "api" and user == "hard":
            raise RuntimeError("timed out")
        return {"by": member.name}

    monkeypatch.setattr(council, "ask", fake_ask)
    members = [CouncilMember(name="api", kind="openai", base_url="http://x", model="m"),
               CouncilMember(name="gpu", kind="ollama", base_url="http://x", model="m", fallback=True)]
    out = council._ask_all(store, members, [("e", "explain", "easy"), ("h", "explain", "hard")], lambda _: None)
    assert out["api"] == {"e": {"by": "api"}, "h": None}
    assert out["gpu"] == {"h": {"by": "gpu"}}             # asked only the question nobody answered
    assert [n for n, _ in asked].count("gpu") == 1

    asked.clear()
    out = council._ask_all(store, members, [("e", "explain", "easy")], lambda _: None)
    assert out["gpu"] == {} and all(n == "api" for n, _ in asked)   # all answered: backup stays idle


def test_approved_ideas_are_bred_and_judged_by_their_carriers(settings, specs):
    store = Store(settings.db_path)
    rows, _ = _sit(settings, store, specs, lambda name, user: (
        {"votes": [{"n": 1, "approve": True, "confidence": 1.0}]} if "Vote on each" in user else
        {"rules": [{"feature": "gmgn_holders", "op": ">=", "value": 80, "kind": "require"}]}))
    idea = next(r for r in rows if r["approved"])
    base = specs["near_graduation"]
    br = Breeder(random.Random(0), Credit([]), {}, {}, ideas={base.id: [idea]})
    sig, ch = br._idea(base)
    assert sig == f"idea:{idea['idea_id']}" and ch.section == "all" and ch.feature == "gmgn_holders"

    # a carrier is born, trades live and loses → the idea fails and its backers lose reputation
    child = br.mutate(base)
    store.insert_evo(dict(ts=time.time() - 10, gen=1, strategy_id=base.id, version="carrier01", parent_version=base.version,
                          mate_version=None, ops=json.dumps([sig]), event="born", fitness=-5, parent_fitness=-6, reason=""))
    run = store.start_run("live")
    for i in range(settings.council.judge_min_trades):
        store.insert_trade(dict(trade_id=f"t{i}", run_id=run, strategy_id=base.id, version="carrier01", mint=f"m{i}",
                                entry_ts=time.time(), exit_ts=time.time() + 1, pnl_sol=-0.01, pnl_pct=-10))
    judged = {i["idea_id"]: i for i in judge_ideas(store, settings, time.time())}
    assert judged[idea["idea_id"]]["status"] == "failed"
    reps = reputations(store)
    assert reps["a"] == -1.0 and reps["b"] == -1.0
    assert child is not None


def test_the_dashboard_reads_sittings_as_chats(settings, specs):
    from solmeme.ui.council_chat import build_council_chat

    store = Store(settings.db_path)
    _sit(settings, store, specs, lambda name, user: (
        {"votes": [{"n": 1, "approve": True, "confidence": 1.0, "reason": "ok"}]} if "Vote on each" in user else
        {"why": "holders", "rules": [{"feature": "gmgn_holders", "op": ">=", "value": 80, "kind": "require"}]}))
    view = build_council_chat(store, settings)
    assert view["sitting"] == view["sittings"][0]["sitting"] and view["sittings"][0]["live"] is False   # closed
    assert view["messages"][0]["phase"] == "open" and view["messages"][-1]["phase"] == "close"
    assert set(view["judges"]) == {"a", "b"}
    last = view["messages"][-1]["id"]
    assert build_council_chat(store, settings, view["sitting"], after_id=last)["messages"] == []   # only newer ones
    assert build_council_chat(store, settings, "no-such-sitting")["sitting"] == view["sitting"]    # falls back to newest


def test_cursor_judges_pace_themselves_to_the_plan(settings, monkeypatch):
    from datetime import datetime

    from solmeme.ui import cursor_usage as cu

    plan = {"used": 600, "remaining": 1400}                 # cents: $6 used, $14 left
    end = datetime(2026, 10, 23, 18, 0).astimezone().isoformat()
    monkeypatch.setattr(cu, "cursor_usage", lambda s, fresh=False: {"account": {"summary": {
        "billingCycleEnd": end, "individualUsage": {"plan": dict(plan)}}}})
    judges = [CouncilMember(name="cursor-sonnet", kind="openai", base_url="http://x", model="m"),
              CouncilMember(name="qwen8b", kind="ollama", base_url="http://x", model="m", fallback=True)]
    morning = datetime(2026, 10, 9, 8, 0).timestamp()        # 14.4 days to the reset: ~$0.97 a day
    sit_, note = council.pace_cursor(settings, judges, now=morning)
    assert [m.name for m in sit_] == ["cursor-sonnet", "qwen8b"] and note == ""
    plan.update(used=690, remaining=1310)                    # $0.90 spent today: still under the share
    assert len(council.pace_cursor(settings, judges, now=morning + 3600)[0]) == 2
    plan.update(used=720, remaining=1280)                    # $1.20 spent: over it
    sit_, note = council.pace_cursor(settings, judges, now=morning + 7200)
    assert [m.name for m in sit_] == ["qwen8b"] and "sit out until tomorrow" in note
    assert len(council.pace_cursor(settings, judges, now=morning + 86400)[0]) == 2   # a new day, a new share
    monkeypatch.setattr(cu, "cursor_usage", lambda s, fresh=False: {"configured": True, "error": "down"})
    assert len(council.pace_cursor(settings, judges, now=morning + 2 * 86400)[0]) == 2   # balance unknown: no pacing


def test_cursor_pacing_uses_each_pools_percentages(settings, monkeypatch):
    # Oct 9: the dollar figure ($7.85 of $20) covers only the API pool. Cursor said 8.7% of the plan was used,
    # yet every Cursor judge was benched for the day. Pace each pool on its own percentage instead.
    from datetime import datetime

    from solmeme.ui import cursor_usage as cu

    plan = {"used": 785, "remaining": 1215, "autoPercentUsed": 10.0, "apiPercentUsed": 7.0}
    monkeypatch.setattr(cu, "cursor_usage", lambda s, fresh=False: {"account": {"summary": {
        "billingCycleEnd": "2026-10-23T12:00:00Z", "individualUsage": {"plan": plan}}}})
    judges = [CouncilMember(name="cursor-sonnet", kind="openai", base_url="http://x", model="claude-sonnet"),
              CouncilMember(name="cursor-auto", kind="openai", base_url="http://x", model="auto"),
              CouncilMember(name="cursor-composer", kind="openai", base_url="http://x", model="composer-2.5-fast"),
              CouncilMember(name="qwen8b", kind="ollama", base_url="http://x", model="m", fallback=True)]
    morning = datetime(2026, 10, 9, 8, 0).timestamp()        # ~14 days left: ~6.4% a day for each pool
    assert len(council.pace_cursor(settings, judges, now=morning)[0]) == 4
    plan.update(used=1000, remaining=1000, autoPercentUsed=14.0)    # $2.15 today, but only 4% of the auto pool
    assert len(council.pace_cursor(settings, judges, now=morning + 3600)[0]) == 4
    plan.update(apiPercentUsed=14.0)                                 # the API pool is past its share
    sit_, note = council.pace_cursor(settings, judges, now=morning + 7200)
    assert [m.name for m in sit_] == ["cursor-auto", "cursor-composer", "qwen8b"] and "api" in note


def test_a_single_judge_cannot_decide_a_whale(settings, monkeypatch):
    from solmeme.lab.whales import WhaleBook

    store = Store(settings.db_path)
    book = WhaleBook(settings.data_dir)
    book.wallets = {"Lonely1111": dict(status="whale", score=1.0, win=0.7, median_exit=1.8, round_trips=9, early_good=3,
                                       tokens=20, label="smart", first_seen=time.time() - 7200, history=[[0, 1.0]])}
    book.save()

    def fake_ask(member, system, user):
        if member.name == "b":
            raise RuntimeError("500 insufficient memory")
        return {"votes": [{"n": 1, "trust": False, "confidence": 0.9, "reason": "too new"}]}

    monkeypatch.setattr(council, "ask", fake_ask)
    settings.council.members = [CouncilMember(name="a", kind="openai", base_url="http://x", model="m"),
                                CouncilMember(name="b", kind="openai", base_url="http://x", model="m")]
    assert council.review_whales(settings, store) == []
    assert WhaleBook(settings.data_dir).wallets["Lonely1111"].get("council") is None   # asked again next time


def test_the_council_decides_which_whales_to_trust(settings, monkeypatch):
    from solmeme.lab.whales import WhaleBook

    store = Store(settings.db_path)
    book = WhaleBook(settings.data_dir)
    base = dict(status="whale", score=1.0, win=0.7, median_exit=1.8, round_trips=9, early_good=3, tokens=20,
                label="smart", first_seen=time.time() - 7200, history=[[0, 0.9], [1, 1.0]])
    book.wallets = {"GoodTrader1111": dict(base), "Insider2222": dict(base, score=0.9), "Maybe3333": dict(base, score=0.8)}
    book.save()

    def fake_ask(member, system, user):
        assert "GoodTrader" in user[:4000] or "Good" in user          # the judges see each wallet's record
        return {"votes": [{"n": 1, "trust": True, "confidence": 0.9, "reason": "skilled"},
                          {"n": 2, "trust": False, "confidence": 0.9, "reason": "launch sniper"},
                          {"n": 3, "trust": member.name == "a", "confidence": 0.8, "reason": "mixed"}]}

    monkeypatch.setattr(council, "ask", fake_ask)
    settings.council.members = [CouncilMember(name="a", kind="openai", base_url="http://x", model="m"),
                                CouncilMember(name="b", kind="openai", base_url="http://x", model="m")]
    out = {o["wallet"]: o["verdict"] for o in council.review_whales(settings, store)}
    assert out == {"GoodTrader1111": "trust", "Insider2222": "distrust", "Maybe3333": "unsure"}
    book = WhaleBook(settings.data_dir)
    assert "Insider2222" not in book.whales()                         # its buys no longer count
    assert book.top()[0] == "GoodTrader1111"                          # trusted whales get polled first
    assert book.to_review(time.time()) == []                          # judged: not asked again for a day
    msgs = store.council_messages(store.council_sittings()[0]["sitting"])
    assert msgs[0]["phase"] == "whales" and msgs[-1]["phase"] == "verdict" and "TRUST" in msgs[-1]["text"]


def test_the_judges_see_what_live_could_not_buy(settings):
    store = Store(settings.db_path)
    run = store.start_run("live")
    tape = []
    for i in range(10):                                   # fast movers (30s +40%) could not be bought; calm ones won
        fast = i < 6
        mint = f"M{i}"
        store.insert_trade(dict(trade_id=f"t{i}", run_id=run, strategy_id="holder_dip", version="v1", mint=mint,
                                entry_ts=100.0 + i, exit_ts=200.0 + i, pnl_sol=0.01, pnl_pct=10,
                                entry_features=json.dumps({"price_chg_30s_pct": 40.0 if fast else 2.0, "gmgn_holders": 60.0})))
        if fast:
            tape.append(dict(side="missed", key="holder_dip@v1", mint=mint, ts=100.0 + i, price=1.4, paper_price=1.0))
        else:
            tape += [dict(side="buy", key="holder_dip@v1", mint=mint, ts=100.0 + i, price=1.05, paper_price=1.0),
                     dict(side="sell", key="holder_dip@v1", mint=mint, ts=150.0 + i, pnl_sol=0.02, partial=False)]
    (settings.data_dir / "session").mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "session" / "dryrun_wallet.json").write_text(json.dumps({"tape": tape}))
    text, rows = council.live_evidence(settings, store, "holder_dip")
    assert "6 could not be bought" in text and "4 won" in text and len(rows) == 10
    assert "price_chg_30s_pct" in text                                   # the feature that separates them
    good_rule = Rule("price_chg_30s_pct", ">=", 20.0, "avoid")
    assert council.live_check(good_rule, rows) == ("live: it would have skipped 6 of 6 entries that failed at "
                                                   "real prices and 0 of 4 that worked")
    assert council.live_evidence(settings, store, "kol_follow") == ("", [])   # no live data for this strategy


def test_local_judges_stay_off_while_cursor_is_up(settings, monkeypatch):
    store = Store(settings.db_path)
    asked = []

    def fake_ask(member, system, user):
        asked.append(member.name)
        if member.name == "9router":
            raise RuntimeError("timed out")
        return {"by": member.name}

    monkeypatch.setattr(council, "ask", fake_ask)
    router = CouncilMember(name="9router", kind="openai", base_url="http://x", model="m", fallback=True)
    gpu = CouncilMember(name="qwen8b", kind="ollama", base_url="http://x", model="m", fallback=True)
    cur = CouncilMember(name="cursor-auto", kind="openai", base_url="http://x", model="auto")
    q = [("e", "explain", "easy")]

    # Cursor benched by today's pacing (not out): 9router leads, the GPU judge never sits
    monkeypatch.setattr(council, "cursor_out", lambda s: "")
    out = council._ask_all(store, [router, gpu], q, lambda _: None, settings=settings)
    assert "qwen8b" not in asked and out["qwen8b"] == {}

    # the Cursor plan is used up: the GPU judge answers what 9router could not
    asked.clear()
    monkeypatch.setattr(council, "cursor_out", lambda s: "the Cursor plan is used up")
    out = council._ask_all(store, [router, gpu], q, lambda _: None, settings=settings)
    assert asked == ["9router", "qwen8b"] and out["qwen8b"]["e"] == {"by": "qwen8b"}

    # Cursor sat and answered: nothing reaches the GPU even if one backup is asked
    asked.clear()
    monkeypatch.setattr(council, "cursor_out", lambda s: "")
    out = council._ask_all(store, [cur, router, gpu], q, lambda _: None, settings=settings)
    assert asked == ["cursor-auto"]


def test_cursor_offline_lets_the_local_judge_answer(settings, monkeypatch):
    store = Store(settings.db_path)

    def fake_ask(member, system, user):
        if member.name.startswith("cursor") or member.name == "9router":
            raise RuntimeError("connection refused")
        return {"by": member.name}

    monkeypatch.setattr(council, "ask", fake_ask)
    monkeypatch.setattr(council, "cursor_out", lambda s: "")
    members = [CouncilMember(name="cursor-auto", kind="openai", base_url="http://x", model="auto"),
               CouncilMember(name="9router", kind="openai", base_url="http://x", model="m", fallback=True),
               CouncilMember(name="qwen8b", kind="ollama", base_url="http://x", model="m", fallback=True)]
    out = council._ask_all(store, members, [("e", "explain", "easy")], lambda _: None, settings=settings)
    assert out["qwen8b"]["e"] == {"by": "qwen8b"}         # every Cursor judge failed: offline in practice


def test_cursor_uses_the_whole_plan_over_the_cycle(settings, monkeypatch):
    # Oct 10 09:50: 55% of the cycle gone, 23% of the plan used, yet the daily share benched every Cursor judge.
    from datetime import datetime

    from solmeme.ui import cursor_usage as cu

    plan = {"autoPercentUsed": 24.4, "apiPercentUsed": 22.1}
    monkeypatch.setattr(cu, "cursor_usage", lambda s, fresh=False: {"account": {"summary": {
        "billingCycleStart": "2026-09-23T18:54:20Z", "billingCycleEnd": "2026-10-23T18:54:20Z",
        "individualUsage": {"plan": plan}}}})
    judges = [CouncilMember(name="cursor-sonnet", kind="openai", base_url="http://x", model="claude-sonnet"),
              CouncilMember(name="cursor-auto", kind="openai", base_url="http://x", model="auto"),
              CouncilMember(name="9router", kind="openai", base_url="http://x", model="m", fallback=True)]
    now = datetime.fromisoformat("2026-10-10T13:50:00+00:00").timestamp()
    sit_, note = council.pace_cursor(settings, judges, now=now)
    assert len(sit_) == 3 and note == ""                         # far under the pace line: everyone sits
    plan.update(apiPercentUsed=58.0)                             # the API pool ran ahead of the line
    sit_, note = council.pace_cursor(settings, judges, now=now)
    assert [m.name for m in sit_] == ["cursor-auto", "9router"] and "api pool 58% used" in note
    later = datetime.fromisoformat("2026-10-12T13:50:00+00:00").timestamp()
    assert len(council.pace_cursor(settings, judges, now=later)[0]) == 3   # the cycle caught up
