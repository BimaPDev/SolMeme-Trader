"""Whale book: wallets that buy good tokens early and exit well become whales, and lose it when they slip."""

from solmeme.core.events import Enrichment, WalletTrade
from solmeme.features.registry import FeatureView
from solmeme.features.token_state import MarketState
from solmeme.lab.whales import WhaleBook, update

W, F, X = "W" * 44, "F" * 44, "X" * 44


def _market(n=8, good=True):
    evs = []
    for i in range(n):
        m, t0 = f"m{i}", 1000 + i * 4000
        caps = [1000, 1500, 4000 if good else 1100, 3500 if good else 900, 900]
        for k, c in enumerate(caps):
            evs.append(Enrichment(ts=t0 + k * 60, source="gmgn_trench", mint=m, data={"mcap_usd": c}))
        evs.append(WalletTrade(ts=t0 + 5, source="gmgn", wallet=W, mint=m, side="buy", label="smartmoney"))
        evs.append(WalletTrade(ts=t0 + 185, source="gmgn", wallet=W, mint=m, side="sell", label="smartmoney"))
        evs.append(WalletTrade(ts=t0 + 190, source="gmgn", wallet=F, mint=m, side="buy", label="kol"))     # buys the top
        evs.append(WalletTrade(ts=t0 + 245, source="gmgn", wallet=F, mint=m, side="sell", label="kol"))
    return sorted(evs, key=lambda e: e.ts)


def test_early_good_exiters_become_whales_and_top_buyers_do_not(settings):
    ch = update(settings, lambda a, b: _market(), (0, 1e9), now=1e6)
    book = WhaleBook(settings.data_dir)
    assert book.wallets[W]["status"] == "whale" and W in book.top()
    assert F not in book.wallets or book.wallets[F]["status"] != "whale"
    assert any(c["wallet"] == W and c["now"] == "whale" for c in ch)


def test_a_whale_that_slips_is_dropped_and_keeps_history(settings):
    update(settings, lambda a, b: _market(), (0, 1e9), now=1e6)
    bad = [e for e in _market(good=False)]
    for e in bad:                                           # now it buys and sells into losers
        if e.kind == "wallet_trade" and e.wallet == W and e.side == "sell":
            e.ts += 60
    update(settings, lambda a, b: bad, (0, 1e9), now=2e6)
    e = WhaleBook(settings.data_dir).wallets[W]
    assert e["status"] == "dropped" and len(e["history"]) == 2


def test_banned_or_linked_wallets_are_never_trusted(settings):
    from solmeme.lab.banlist import BanList
    update(settings, lambda a, b: _market(), (0, 1e9), now=1e6, trace=lambda w, banned: ["HUB"])
    assert WhaleBook(settings.data_dir).wallets[W]["flag"] and W not in WhaleBook(settings.data_dir).whales()
    b = BanList(settings.data_dir); b.add(X, "cluster", "x"); b.save()


def test_whale_buys_feature_counts_recent_whales():
    m = MarketState()
    m.whales = {W}
    st = m.apply(WalletTrade(ts=100.0, source="gmgn", wallet=W, mint="M" * 43, side="buy"))
    m.apply(WalletTrade(ts=101.0, source="gmgn", wallet=F, mint="M" * 43, side="buy"))
    assert FeatureView(st, 200.0, m).get("whale_buys") == 1
    assert FeatureView(st, 800.0, m).get("whale_buys") == 0


def test_the_scan_counts_each_wallets_own_launches():
    from solmeme.core.events import TokenLaunched
    from solmeme.lab import whales

    evs = [TokenLaunched(ts=float(i), source="pumpportal", mint=f"M{i}pump", creator="DevWallet") for i in range(3)]
    whales.scan(evs)
    assert whales.CREATED["DevWallet"] == 3
    from solmeme.ai.council import _whale_line
    line = _whale_line("DevWallet" + "x" * 31, {"score": 1.0, "created": 3, "round_trips": 9}, 0.0)
    assert "launched 3 coins itself" in line
