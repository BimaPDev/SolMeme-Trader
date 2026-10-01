"""Regressions found in review of the platform adapters."""

from solmeme.sources import axiom, fomo, j7tracker

MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"


def test_axiom_curve_venues_are_not_migrated():
    for proto in ("Raydium LaunchLab", "Meteora DBC"):
        assert axiom._stage({"protocol": proto, "bonding_pct": 40}, room=None, threshold=75) == "axiom_new_pair"
    assert axiom._stage({"protocol": "Raydium CPMM"}, room=None, threshold=75) == "axiom_migrated"
    assert axiom._stage({"protocol": "Pump AMM"}, room=None, threshold=75) == "axiom_migrated"


def test_fomo_non_list_leaderboard_response_keeps_followed_set():
    class Ctx:  # minimal stand-in; Source only needs settings/watch for options and emit
        pass
    src = fomo.Source.__new__(fomo.Source)
    src.options, src._seen, src.followed = {}, set(), fomo.Followed()
    src.sample = lambda *a, **k: None
    src._apply_traders([{"id": "1", "address": MINT, "handle": "alice"}])
    src.on_http("https://prod-api.fomo.family/v2/leaderboard/stats", {"totalTraders": 1234})
    assert MINT in src.followed.wallets


def test_j7_signature_slices_are_not_mints():
    sig = "5" + "VfYmGx3Kd2pQ8rT7wLz9NbH4cJ6sA1eF" * 3   # 97 base58 chars, like a tx signature
    assert j7tracker.extract_mints(f"look solscan.io/tx/{sig}") == []
    assert j7tracker.extract_mints(f"CA: {MINT} 🚀") == [MINT]
