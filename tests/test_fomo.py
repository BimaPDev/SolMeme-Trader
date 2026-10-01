"""fomo.family leaderboard, fills, and feature tests.

Fixtures are reconstructed shapes (see each file's ``_note``), not live captures.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from solmeme.config import Toggle
from solmeme.core.events import ExternalSignal, TokenLaunched, WalletTrade
from solmeme.features.registry import FEATURES, FeatureView
from solmeme.features.token_state import MarketState
from solmeme.sources.base import Context
from solmeme.sources.fomo import (
    Followed,
    Source,
    leaderboard_window,
    parse_feed_item,
    parse_http,
    parse_leaderboard,
    parse_swap,
    parse_trending,
    parse_ws_message,
)

FIX = Path(__file__).resolve().parent / "fixtures" / "fomo"

W1 = "LbUiWL3xVV8hTFYBVdbTNrpDo41NKS6o3LHHuDzjfcY"
W2 = "QWmroo4YnnMqYW3cnxWkFdaTxGD3P7vMSzwMHGbUzwF"
W3 = "US517G5965aydkZ46HS38QLi7UQiSojurfbQfKCELFx"
MINT = "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"

URL_7D = "https://prod-api.fomo.family/v2/leaderboard/7d"
URL_24H = "https://prod-api.fomo.family/v2/leaderboard/24h"
URL_BARE = "https://prod-api.fomo.family/v2/leaderboard"
URL_SWAPS = "https://prod-api.fomo.family/v2/users/u1/swaps"
URL_FEED = "https://prod-api.fomo.family/v2/feed/items"
URL_TREND = "https://prod-api.fomo.family/proxy/trendingTokens"
WS = "wss://prod-api.fomo.family/ws"


def load(name: str):
    return json.loads((FIX / name).read_text())


def make_source(settings, **opts) -> Source:
    settings.sources.fomo = Toggle(**opts) if opts else Toggle()
    ctx = Context(settings=settings, http=httpx.AsyncClient(), emit=lambda ev: None)
    return Source(ctx)


def primed(settings, **opts) -> Source:
    """Leaderboard applied with top_n=1, so only moon_otter (W1) is followed."""
    params = {"top_n": 1, "leaderboard": "7d", "min_usd": 20}
    params.update(opts)
    src = make_source(settings, **params)
    assert src.on_http(URL_7D, load("leaderboard.json")) == []
    assert src.followed.wallets == {W1}
    return src


def test_fixtures_note_reconstructed_shape():
    files = list(FIX.glob("*.json"))
    assert files
    for path in files:
        note = json.loads(path.read_text()).get("_note", "")
        assert "RECONSTRUCTED" in note
        assert "itsnex1s/fomo-sdk" in note
        assert "not a live capture" in note


def test_leaderboard_window():
    assert leaderboard_window("https://fomo.family/leaderboard") == ""
    assert leaderboard_window("https://prod-api.fomo.family/v2/leaderboard") == ""
    assert leaderboard_window("https://prod-api.fomo.family/v2/leaderboard/7d") == "7d"
    assert leaderboard_window("https://prod-api.fomo.family/v2/leaderboard/7d?limit=50") == "7d"
    assert leaderboard_window("https://prod-api.fomo.family/v2/leaderboard/24h") == "24h"
    assert leaderboard_window("https://prod-api.fomo.family/v2/leaderboard/30d") == "30d"
    assert leaderboard_window("https://prod-api.fomo.family/v2/leaderboard/all-time") == "all-time"
    assert leaderboard_window("https://prod-api.fomo.family/v2/users/u1/swaps") is None
    assert leaderboard_window("https://fomo.family/") is None


def test_leaderboard_followed_set_top_n_and_window(settings):
    board = load("leaderboard.json")
    alt = load("leaderboard_alt.json")
    inner = board["responseObject"]

    rows = parse_leaderboard(board)
    assert rows == [
        {"id": "u1", "address": W1, "handle": "moon_otter"},
        {"id": "u2", "address": W2, "handle": "pixel_monk"},
    ]
    assert parse_leaderboard({"data": inner}) == rows
    assert parse_leaderboard({"result": inner}) == rows

    events, recognized, traders = parse_http(URL_7D, board, Followed(), min_usd=20)
    assert events == [] and recognized is True and traders == rows

    src = make_source(settings, top_n=1, leaderboard="7d", min_usd=20)
    assert src.on_http(URL_7D, board) == []
    assert src.followed.wallets == {W1}
    assert src.followed.ids == {"u1": W1}
    assert src.followed.handles == {"moon_otter": W1}
    assert len(src.followed) == 1

    assert src.on_http(URL_24H, alt) == []
    assert src.followed.wallets == {W1}
    assert src.followed.ids == {"u1": W1}

    assert src.on_http(URL_BARE, alt) == []
    assert src.followed.wallets == {W2}
    assert src.followed.ids == {"u2": W2}
    assert src.followed.handles == {"pixel_monk": W2}

    assert src.on_http(URL_7D, board) == []
    assert src.followed.wallets == {W1}


def test_default_top_n_follows_both(settings):
    src = make_source(settings)
    assert src.on_http(URL_7D, load("leaderboard.json")) == []
    assert src.followed.wallets == {W1, W2}
    assert src.on_http(URL_24H, load("leaderboard_alt.json")) == []
    assert src.followed.wallets == {W1, W2}


def test_swaps_keep_solana_fill_drop_rest_and_dedupe(settings):
    src = primed(settings)
    data = load("swaps.json")
    parsed, recognized, traders = parse_http(URL_SWAPS, data, src.followed, min_usd=20)
    assert recognized is True and traders is None
    assert [e.tx for e in parsed] == ["sig-buy-w1", "sig-sell-w1", "sig-buy-w1"]
    assert parsed[0].side == "buy" and parsed[0].usd == 42.5
    assert parsed[1].side == "sell" and parsed[1].usd == 55

    events = src.on_http(URL_SWAPS, data)
    assert [(e.tx, e.side, e.usd, e.wallet, e.mint, e.source, e.label) for e in events] == [
        ("sig-buy-w1", "buy", 42.5, W1, MINT, "fomo", "fomo"),
        ("sig-sell-w1", "sell", 55, W1, MINT, "fomo", "fomo"),
    ]
    assert all(isinstance(e, WalletTrade) for e in events)
    assert {e.tx for e in events}.isdisjoint({"sig-dust", "sig-stranger", "sig-cross-robinhood", "sig-quote"})
    assert src.on_http(URL_SWAPS, data) == []

    strict = primed(settings, min_usd=50)
    assert [(e.tx, e.side) for e in strict.on_http(URL_SWAPS, data)] == [("sig-sell-w1", "sell")]


def test_feed_skips_multi_user_emits_swap_buy_by_user_id(settings):
    src = primed(settings)
    events = src.on_http(URL_FEED, load("feed.json"))
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, WalletTrade)
    assert ev.side == "buy" and ev.wallet == W1 and ev.mint == MINT
    assert ev.tx == "feed-buy-u1" and ev.usd == 64 and ev.symbol == "OTTR"
    assert ev.source == "fomo" and ev.label == "fomo"
    assert parse_feed_item({"type": "multi_user_buy"}, src.followed) is None
    assert parse_feed_item({"type": "multi_user_sell"}, src.followed) is None


def test_trending_keeps_solana_drops_base(settings):
    src = make_source(settings, top_n=1, leaderboard="7d", min_usd=20)
    events, recognized, traders = parse_http(URL_TREND, load("trending.json"), Followed(), min_usd=20)
    assert recognized is True and traders is None and len(events) == 1
    got = src.on_http(URL_TREND, load("trending.json"))
    assert len(got) == 1
    ev = got[0]
    assert isinstance(ev, ExternalSignal)
    assert ev.source == "fomo" and ev.mint == MINT and ev.signal == "fomo_trending"
    assert ev.data["fomo_rank"] == 1
    assert ev.data["fomo_buyers"] == 17
    assert ev.data["symbol"] == "OTTR"
    assert ev.data["mcap_usd"] == 250000
    assert ev.data["holders"] == 88
    assert parse_trending(load("trending.json"))[0].mint == MINT


def test_ws_data_frames_and_control(settings):
    src = primed(settings)
    events, recognized = parse_ws_message(load("ws_trading.json"), src.followed, min_usd=20)
    assert recognized is True and len(events) == 1
    assert events[0].side == "buy" and events[0].wallet == W1 and events[0].tx == "ws-buy-u1"

    got = src.on_ws(WS, load("ws_trading.json"))
    assert len(got) == 1 and got[0].label == "fomo" and got[0].source == "fomo"
    assert src.on_ws(WS, load("ws_trading.json")) == []

    trend, recognized = parse_ws_message(load("ws_trending.json"), src.followed, min_usd=20)
    assert recognized is True and len(trend) == 1 and trend[0].mint == MINT
    assert trend[0].signal == "fomo_trending" and trend[0].data["fomo_rank"] == 1
    via_source = src.on_ws(WS, load("ws_trending.json"))
    assert len(via_source) == 1 and via_source[0].mint == MINT

    for name in ("ws_challenge.json", "ws_challenge_accepted.json", "ws_subscribed.json"):
        assert parse_ws_message(load(name), src.followed) == ([], True)
        assert src.on_ws(WS, load(name)) == []


def test_garbage_returns_empty(settings):
    src = make_source(settings, top_n=1, leaderboard="7d", min_usd=20)
    assert src.on_http("https://prod-api.fomo.family/v2/nope", load("garbage.json")) == []
    assert parse_http("https://prod-api.fomo.family/v2/nope", load("garbage.json"), Followed(), min_usd=20) == ([], False, None)
    assert parse_ws_message(load("ws_unknown.json"), Followed()) == ([], False)
    assert src.on_ws(WS, load("ws_unknown.json")) == []
    assert src.on_ws(WS, "garbage") == []
    assert parse_ws_message("garbage", Followed()) == ([], False)
    assert parse_leaderboard(None) == []
    assert parse_trending(None) == []
    assert parse_swap(None, Followed()) is None
    assert parse_feed_item([], Followed()) is None
    assert parse_http("", None, Followed(), min_usd=20) == ([], False, None)


def test_features_and_kol_buys_600s_count_fomo():
    for name in ("fomo_trader_buys", "fomo_trader_buys_600s", "fomo_trader_sells", "fomo_trending"):
        assert FEATURES[name].group == "fomo"

    unseen = MarketState()
    unseen.apply(TokenLaunched(ts=0, mint=MINT, symbol="OTTR"))
    blank = FeatureView(unseen.get(MINT), 0, unseen)
    assert blank.get("fomo_trader_buys") is None
    assert blank.get("fomo_trader_buys_600s") is None
    assert blank.get("fomo_trader_sells") is None
    assert blank.get("fomo_trending") is None
    assert blank.get("kol_buys_600s") == 0

    ms = MarketState()
    ms.apply(WalletTrade(ts=0, source="fomo", label="fomo", wallet=W1, mint=MINT, side="buy", usd=40, tx="a"))
    st = ms.get(MINT)
    assert FeatureView(st, 0, ms).get("kol_buys_600s") == 1
    assert FeatureView(st, 0, ms).get("fomo_trader_buys") == 1
    assert FeatureView(st, 0, ms).get("fomo_trader_buys_600s") == 1
    assert FeatureView(st, 0, ms).get("fomo_trader_sells") == 0
    assert FeatureView(st, 0, ms).get("fomo_trending") is False

    ms.apply(WalletTrade(ts=0, source="fomo", label="fomo", wallet=W1, mint=MINT, side="buy", usd=10, tx="b"))
    assert FeatureView(st, 0, ms).get("fomo_trader_buys") == 1
    ms.apply(WalletTrade(ts=0, source="fomo", label="fomo", wallet=W2, mint=MINT, side="buy", usd=30, tx="c"))
    assert FeatureView(st, 0, ms).get("fomo_trader_buys") == 2
    assert FeatureView(st, 0, ms).get("kol_buys_600s") == 2
    ms.apply(WalletTrade(ts=0, source="gmgn", label="kol", wallet=W3, mint=MINT, side="buy", usd=15, tx="kol"))
    assert FeatureView(st, 0, ms).get("kol_buys_600s") == 3
    assert FeatureView(st, 0, ms).get("fomo_trader_buys") == 2
    ms.apply(WalletTrade(ts=0, source="fomo", label="fomo", wallet=W1, mint=MINT, side="sell", usd=20, tx="d"))
    assert FeatureView(st, 0, ms).get("fomo_trader_sells") == 1
    assert FeatureView(st, 601, ms).get("fomo_trader_buys_600s") == 0
    assert FeatureView(st, 601, ms).get("fomo_trader_buys") == 2
    assert FeatureView(st, 601, ms).get("kol_buys_600s") == 0

    sold = MarketState()
    sold.apply(WalletTrade(ts=0, source="fomo", label="fomo", wallet=W1, mint=MINT, side="sell", usd=22))
    sold_view = FeatureView(sold.get(MINT), 0, sold)
    assert sold_view.get("fomo_trader_buys") == 0
    assert sold_view.get("fomo_trader_sells") == 1
    assert sold_view.get("fomo_trending") is False

    trend = MarketState()
    trend.apply(ExternalSignal(
        ts=0, source="fomo", mint=MINT, signal="fomo_trending", data={"fomo_rank": 1, "symbol": "OTTR"}))
    trend_view = FeatureView(trend.get(MINT), 0, trend)
    assert trend_view.get("fomo_trending") is True
    assert trend_view.get("fomo_trader_buys") == 0
    assert trend_view.get("fomo_trader_sells") == 0
