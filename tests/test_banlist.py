"""Ban list: detection of coordinated clusters and rug creators, live flagging, prefilter block."""

import time

from solmeme.core.events import Enrichment, TokenLaunched, WalletTrade
from solmeme.features.registry import FeatureView
from solmeme.features.token_state import MarketState
from solmeme.lab.banlist import BanList, candidates, find_clusters, find_rug_creators, update

A, B, C, D = ("A" * 44, "B" * 44, "C" * 44, "D" * 44)


def test_clusters_are_wallets_that_always_buy_together():
    buys = []
    for i in range(9):
        t = 1000 + i * 600
        buys += [(t, A, f"m{i}"), (t + 3, B, f"m{i}"), (t + 9, C, f"m{i}")]
        buys += [(t + 200, D, f"m{i}")]                      # same tokens, minutes later: not the cluster
    out = find_clusters(buys)
    assert len(out) == 1 and out[0]["wallets"] == sorted([A, B, C]) and out[0]["shared_tokens"] == 9
    assert find_clusters([b for b in buys if b[2] in ("m0", "m1", "m2", "m3", "m4")]) == []   # 5 shared: not enough


def test_rug_creator_is_a_pump_then_a_crash():
    creators = {"rug": (A, 0.0), "ok": (B, 0.0)}
    creators["tiny"] = (C, 0.0)
    mcap = {"rug": [(t, m) for t, m in [(10, 5000), (60, 9000), (300, 40000), (310, 42000), (360, 1500), (400, 1400)]],
            "tiny": [(t, m) for t, m in [(10, 300), (60, 800), (90, 2500), (95, 100), (120, 90)]],
            "ok": [(t, m) for t, m in [(10, 5000), (60, 9000), (300, 40000), (360, 30000), (400, 25000)]]}
    out = find_rug_creators(creators, mcap)
    assert [r["creator"] for r in out] == [A]


def test_banned_wallets_flag_tokens_and_the_prefilter_blocks_them(settings):
    m = MarketState()
    m.banned = {A, B}
    st = m.apply(TokenLaunched(ts=1.0, source="pumpportal", mint="M" * 43, pool="pump", creator=A, v_sol=30.0, v_tokens=1.073e9))
    assert FeatureView(st, 2.0, m).get("banned_wallets") == 1
    m.apply(WalletTrade(ts=3.0, source="gmgn_smartmoney", wallet=B, mint="M" * 43, side="buy"))
    m.apply(WalletTrade(ts=4.0, source="gmgn_smartmoney", wallet=C, mint="M" * 43, side="buy"))
    assert FeatureView(st, 5.0, m).get("banned_wallets") == 2
    from solmeme.config import load_settings
    assert any(r.get("id") == "banned" and r.get("f") == "banned_wallets" for r in load_settings().prefilter.none)


def test_update_bans_directly_without_council_and_remembers(settings):
    settings.council.enabled = False
    evs = []
    for i in range(9):
        t = 1000 + i * 600
        for k, w in enumerate((A, B, C)):
            evs.append(WalletTrade(ts=t + k, source="gmgn_smartmoney", wallet=w, mint=f"m{i}", side="buy"))
    decisions = update(settings, None, lambda a, b: evs, (0, 1e10), now=time.time())
    assert decisions and decisions[0]["banned"]
    bans = BanList(settings.data_dir)
    assert {A, B, C} <= bans.addresses and bans.bans[A].source == "auto"
    assert update(settings, None, lambda a, b: evs, (0, 1e10)) == []          # nothing new the second time


def test_council_rejection_is_remembered(settings, monkeypatch):
    from solmeme.ai import council
    from solmeme.config import CouncilMember
    settings.council.enabled = True
    settings.council.members = [CouncilMember(name="j", kind="openai", base_url="http://x", model="m")]
    monkeypatch.setattr(council, "ask", lambda m, s, u: {"votes": [{"n": 1, "ban": False, "confidence": 0.9, "reason": "just fast traders"}]})
    evs = [WalletTrade(ts=1000 + i * 600 + k, source="gmgn", wallet=w, mint=f"m{i}", side="buy")
           for i in range(9) for k, w in enumerate((A, B))]
    from solmeme.core.store import Store
    d = update(settings, Store(settings.db_path), lambda a, b: evs, (0, 1e10))
    assert d and not d[0]["banned"] and "just fast traders" in d[0]["council"]
    bans = BanList(settings.data_dir)
    assert not bans.addresses and bans.recently_rejected(A)
    assert candidates(bans, [(e.ts, e.wallet, e.mint) for e in evs], {}, {}, time.time()) == []


def test_prune_drops_auto_bans_the_stricter_rules_would_not_make(settings):
    from solmeme.lab.banlist import prune
    b = BanList(settings.data_dir)
    b.add(A, "creator", "tiny", source="council", evidence={"peak_mcap": 544, "crash_s": 2})
    b.add(B, "creator", "real", source="council", evidence={"peak_mcap": 137000, "crash_s": 54})
    b.add(C, "cluster", "small", source="auto", evidence={"shared_tokens": 5})
    b.add(D, "creator", "manual", source="manual", evidence={})
    assert sorted(prune(b)) == sorted([A, C]) and b.addresses == {B, D}


def test_council_reviews_suspects_in_small_batches(settings, monkeypatch):
    from solmeme.ai import council
    from solmeme.config import CouncilMember
    from solmeme.core.store import Store
    settings.council.enabled = True
    settings.council.members = [CouncilMember(name="j", kind="ollama", base_url="http://x", model="m")]
    seen = []

    def fake(member, system, user):
        n = user.count("\n  ")                                   # suspects listed in this question
        seen.append((n, len(user)))
        return {"votes": [{"n": k + 1, "ban": k % 2 == 0, "confidence": 0.9} for k in range(n)]}

    monkeypatch.setattr(council, "_ask_once", fake)
    cands = [dict(kind="rug_creator", wallets=[f"W{i}"], role="creator", reason="x",
                  evidence=dict(start_mcap=1000, peak_mcap=20000, low_after=50, crash_s=40)) for i in range(13)]
    store = Store(settings.db_path)
    out = council.review_bans(settings, store, cands)
    assert [n for n, _ in seen] == [6, 6, 1]                     # 13 suspects → 3 questions
    msgs = store.council_messages(store.council_sittings()[0]["sitting"])
    assert [m["phase"] for m in msgs][0] == "bans" and msgs[-1]["phase"] == "verdict"
    assert sum(m["speaker"] == "j" and m["kind"] == "answer" for m in msgs) == 3
    assert "#1 BAN" in msgs[-1]["text"] and "#2 keep" in msgs[-1]["text"]
    assert sorted(out) == list(range(13))                        # every suspect mapped back to its own vote
    assert out[0][0] is True and out[1][0] is False and out[12][0] is True
    assert all(size < council.MAX_LOCAL_PROMPT_CHARS for _, size in seen)


def test_over_long_prompts_are_trimmed_for_local_models():
    from solmeme.ai.council import MAX_LOCAL_PROMPT_CHARS, fit_prompt
    from solmeme.config import CouncilMember
    local = CouncilMember(name="l", kind="ollama", base_url="http://x", model="m")
    router = CouncilMember(name="r", kind="openai", base_url="http://x", model="m")
    user = "EVIDENCE " * 5000 + "Answer with JSON {votes}"
    out = fit_prompt(local, "sys", user)
    assert len(out) <= MAX_LOCAL_PROMPT_CHARS and out.endswith("Answer with JSON {votes}") and "trimmed" in out
    assert fit_prompt(router, "sys", user) == user
