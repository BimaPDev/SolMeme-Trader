"""Parser tests for the j7tracker.io adapter.

Fixtures are reconstructed examples of the socket.io shapes the adapter
documents. They are not captures from a live session.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from solmeme.config import Toggle
from solmeme.core.events import ExternalSignal
from solmeme.features.registry import FEATURES, FeatureView
from solmeme.features.token_state import MarketState, TokenState
from solmeme.sources.base import Context
from solmeme.sources.j7tracker import (
    MintStats,
    Source,
    b58decode,
    extract_mints,
    is_solana_mint,
    parse_ws_message,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "j7tracker"
MINT = "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"
MINT2 = "8qbHbw2BbbTHBW1sbeqakYXVKRQM8Ne7pLK7m6CVfeR"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
USD1 = "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB"
SYSTEM = "1" * 32
WS = "wss://nyc.j7tracker.io/socket.io/?EIO=4&transport=websocket"
LOOKALIKE = "wss://j7tracker.com/socket.io/?EIO=4&transport=websocket"
SOCIAL = ("j7_tweeted", "j7_tweet_count", "j7_max_followers", "j7_secs_since_first_tweet")


def load(name: str) -> dict:
    data = json.loads((FIXTURES / name).read_text())
    note = data["note"].lower()
    assert "reconstructed" in note and "generic" in note
    return data


def frame(name: str) -> str:
    return load(name)["frame"]


def source(settings, **opts) -> Source:
    settings.sources.j7tracker = Toggle(**opts)
    return Source(Context(settings, None, lambda e: None))


def samples(settings) -> Path:
    return settings.data_dir / "browser" / "j7tracker" / "samples.jsonl"


def test_fixtures_are_reconstructed():
    files = list(FIXTURES.glob("*.json"))
    assert files
    for path in files:
        note = json.loads(path.read_text())["note"].lower()
        assert "reconstructed" in note and "generic" in note


def test_mint_validation():
    assert len(b58decode(MINT)) == 32 and is_solana_mint(MINT)
    assert len(b58decode(MINT2)) == 32 and is_solana_mint(MINT2)
    assert b58decode("0" * 32) is None and not is_solana_mint("0" * 32)
    assert len(b58decode("1" * 44)) == 44 and not is_solana_mint("1" * 44)
    assert len(b58decode(SYSTEM)) == 32 and not is_solana_mint(SYSTEM)
    for quote in (WSOL, USDC, USDT, USD1):
        assert not is_solana_mint(quote)
    assert not is_solana_mint(None)
    events, recognized = parse_ws_message(frame("tweet_invalid.json"))
    assert events == [] and recognized is True


def test_valid_tweet_signal():
    fix = load("tweet_valid.json")
    assert fix["frame"] == "42" + json.dumps(["tweet", fix["tweet"]], separators=(",", ":"))
    assert len(fix["text"]) > 200
    events, recognized = parse_ws_message(fix["frame"])
    assert recognized is True and len(events) == 1
    ev = events[0]
    assert ev.kind == "signal" and ev.source == "j7tracker" and ev.signal == "j7_tweet_ca"
    assert ev.mint == MINT
    assert ev.data["author"] == "alice"
    assert ev.data["followers"] == 2500
    assert ev.data["tweet_id"] == "tw-valid"
    assert ev.data["is_reply"] is True
    assert ev.data["is_retweet"] is False
    assert ev.data["text"] == fix["text"][:200]
    assert len(ev.data["text"]) == 200
    assert ev.data["tweet_count"] == 1
    assert ev.data["max_followers"] == 2500
    again, recognized = parse_ws_message(fix["frame"].encode())
    assert recognized is True and again[0].mint == MINT
    as_json, recognized = parse_ws_message(json.dumps(fix["tweet"]))
    assert recognized is True and as_json[0].mint == MINT
    wrapped, recognized = parse_ws_message({"event": "tweet", "data": fix["tweet"]})
    assert recognized is True and wrapped[0].mint == MINT


def test_tweet_event_aliases():
    tweet = load("tweet_valid.json")["tweet"]
    for name in ("tweet", "tweet_update", "tweet.subtweet.update", "quoted_tweet", "reply_tweet"):
        raw = "42" + json.dumps([name, tweet], separators=(",", ":"))
        events, recognized = parse_ws_message(raw)
        assert recognized is True and len(events) == 1, name
        assert events[0].mint == MINT and events[0].signal == "j7_tweet_ca"


def test_pump_fun_url_extraction():
    fix = load("tweet_pump_url.json")
    assert extract_mints(fix["text"]) == [MINT]
    events, recognized = parse_ws_message(fix["frame"])
    assert recognized is True and len(events) == 1 and events[0].mint == MINT
    assert "pump.fun/coin/" in events[0].data["text"]


def test_solana_token_urls():
    fix = load("tweet_urls.json")
    assert fix["urls"]
    for url in fix["urls"]:
        assert extract_mints(url) == [MINT], url
    events, recognized = parse_ws_message(fix["frame"])
    assert recognized is True and [e.mint for e in events] == [MINT]


def test_multiple_cas_emit_two_events():
    events, recognized = parse_ws_message(frame("tweet_two.json"))
    assert recognized is True
    assert [e.mint for e in events] == [MINT, MINT2]
    assert {e.signal for e in events} == {"j7_tweet_ca"}
    assert events[0].data["tweet_id"] == events[1].data["tweet_id"] == "tw-two"
    assert events[0].data["tweet_count"] == 1
    assert events[1].data["tweet_count"] == 1


def test_wsol_and_usdc_ignored():
    assert extract_mints(f"{WSOL} {USDC}") == []
    assert extract_mints(f"https://pump.fun/coin/{WSOL}") == []
    assert extract_mints(f"https://solscan.io/token/{SYSTEM}") == []
    assert extract_mints(f"{WSOL} {MINT} {USDC}") == [MINT]
    fix = load("tweet_ignored.json")
    events, recognized = parse_ws_message(fix["frame"])
    assert events == [] and recognized is True
    events, recognized = parse_ws_message(fix["frame_with_mint"])
    assert recognized is True and [e.mint for e in events] == [MINT]


def test_nested_fields_are_scanned():
    fix = load("tweet_nested.json")
    events, recognized = parse_ws_message(fix["quoted_and_card"])
    assert recognized is True
    assert [e.mint for e in events] == [MINT2, MINT]
    assert all(e.data["text"] == "quoting this" and e.data["is_reply"] is True for e in events)

    events, recognized = parse_ws_message(fix["reply_to"])
    assert recognized is True and [e.mint for e in events] == [MINT2]
    assert events[0].data["text"] == "thanks"

    events, recognized = parse_ws_message(fix["tweet_url"])
    assert recognized is True and [e.mint for e in events] == [MINT]
    assert events[0].data["text"] == "see profile"

    events, recognized = parse_ws_message(fix["card_title_description"])
    assert recognized is True and [e.mint for e in events] == [MINT, MINT2]
    assert events[0].data["text"] == "look"


def test_initial_tweets_array():
    fix = load("initial_tweets.json")
    for raw in (fix["frame"], fix["payload"]):
        events, recognized = parse_ws_message(raw)
        assert recognized is True and len(events) == 2
        assert [e.data["tweet_id"] for e in events] == ["init-1", "init-2"]
        assert [e.data["tweet_count"] for e in events] == [1, 2]
        assert [e.data["max_followers"] for e in events] == [100, 4000]
        assert all(e.mint == MINT and e.signal == "j7_tweet_ca" for e in events)


def test_dedupe_does_not_bump_tweet_count():
    stats = MintStats()
    raw = frame("tweet_first.json")
    first, recognized = parse_ws_message(raw, stats=stats)
    assert recognized is True and first[0].data["tweet_count"] == 1
    assert first[0].data["author"] == "Alice"
    assert first[0].data["followers"] == 5000
    again, recognized = parse_ws_message(raw, stats=stats)
    assert again == [] and recognized is True
    assert len(stats.tweets[MINT]) == 1
    assert stats.max_followers[MINT] == 5000


def test_second_tweet_id_updates_count_and_max_followers():
    stats = MintStats()
    first_raw = frame("tweet_first.json")
    first, _ = parse_ws_message(first_raw, stats=stats)
    assert first[0].data["tweet_count"] == 1
    assert parse_ws_message(first_raw, stats=stats)[0] == []
    second, recognized = parse_ws_message(frame("tweet_second.json"), stats=stats)
    assert recognized is True and len(second) == 1
    assert second[0].data["tweet_id"] == "tweet-2"
    assert second[0].data["tweet_count"] == 2
    assert second[0].data["followers"] == 1200
    assert second[0].data["max_followers"] == 5000
    assert second[0].data["is_retweet"] is True


def test_source_keeps_mint_stats(settings):
    src = source(settings)
    first = sigs(src.on_ws(WS, frame("tweet_first.json")))
    again = sigs(src.on_ws(WS, frame("tweet_first.json")))
    second = sigs(src.on_ws(WS, frame("tweet_second.json")))
    assert first[0].data["tweet_count"] == 1 and first[0].mint == MINT
    assert again == []
    assert second[0].data["tweet_count"] == 2
    assert second[0].data["max_followers"] == 5000
    assert not samples(settings).exists()


def test_min_followers_filter():
    low, recognized = parse_ws_message(frame("tweet_low.json"), min_followers=1000)
    exact, _ = parse_ws_message(frame("tweet_exact.json"), min_followers=1000)
    missing, recognized_missing = parse_ws_message(frame("tweet_no_followers.json"), min_followers=1000)
    kept, _ = parse_ws_message(frame("tweet_no_followers.json"), min_followers=0)
    assert low == [] and recognized is True
    assert len(exact) == 1 and exact[0].data["followers"] == 1000
    assert missing == [] and recognized_missing is True
    assert len(kept) == 1 and "followers" not in kept[0].data
    stats = MintStats()
    assert parse_ws_message(frame("tweet_low.json"), min_followers=1000, stats=stats)[0] == []
    high, _ = parse_ws_message(frame("tweet_first.json"), min_followers=1000, stats=stats)
    assert high[0].data["tweet_count"] == 1


def test_authors_filter():
    first = frame("tweet_first.json")
    second = frame("tweet_second.json")
    assert len(parse_ws_message(first, authors=["alice"])[0]) == 1
    assert parse_ws_message(second, authors=["alice"])[0] == []
    assert len(parse_ws_message(first, authors=["@ALICE"])[0]) == 1
    assert len(parse_ws_message(second, authors=["BOB"])[0]) == 1
    assert len(parse_ws_message(first, authors="alice")[0]) == 1
    assert len(parse_ws_message(first, authors="alice, bob")[0]) == 1
    assert len(parse_ws_message(second, authors="@Alice, BOB")[0]) == 1
    assert parse_ws_message(first, authors="bob")[0] == []
    for authors in (None, [], ""):
        events, recognized = parse_ws_message(second, authors=authors)
        assert recognized is True and len(events) == 1


def sigs(evs):
    return [e for e in evs if e.kind == "signal"]          # on_ws also returns every tweet as a narrative Headline


def test_source_returns_tweets_as_headlines(settings):
    evs = source(settings).on_ws(WS, frame("tweet_valid.json"))
    assert {e.kind for e in evs} == {"signal", "headline"}


def test_source_author_string(settings):
    src = source(settings, authors="alice, bob")
    assert len(sigs(src.on_ws(WS, frame("tweet_first.json")))) == 1
    assert len(sigs(src.on_ws(WS, frame("tweet_second.json")))) == 1
    assert src.on_ws(WS, frame("tweet_low.json"))[0].data["author"] == "alice"


def test_source_toggle_options(settings):
    settings.sources.j7tracker = Toggle(min_followers=1000, authors=["alice"])
    src = Source(Context(settings, None, lambda e: None))
    kept = sigs(src.on_ws(WS, frame("tweet_first.json")))
    low = sigs(src.on_ws(WS, frame("tweet_low.json")))
    other = sigs(src.on_ws(WS, frame("tweet_second.json")))
    unknown = sigs(src.on_ws(WS, frame("tweet_no_followers.json")))
    exact = sigs(src.on_ws(WS, frame("tweet_exact.json")))
    assert len(kept) == 1 and kept[0].data["author"] == "Alice"
    assert kept[0].data["tweet_count"] == 1
    assert low == [] and other == [] and unknown == []
    assert len(exact) == 1 and exact[0].data["tweet_count"] == 2
    assert exact[0].data["max_followers"] == 5000
    assert not samples(settings).exists()


def test_token_meta(settings):
    fix = load("token_meta.json")
    events, recognized = parse_ws_message(fix["frame"])
    assert recognized is True and len(events) == 1
    ev = events[0]
    assert ev.signal == "j7_tweet_ca" and ev.mint == MINT and ev.source == "j7tracker"
    assert ev.data["tweet_id"] == "meta-1"
    assert ev.data["author"] == ""
    assert ev.data["is_reply"] is False and ev.data["is_retweet"] is False
    assert ev.data["text"] == ""
    assert ev.data["symbol"] == "PEPE"
    assert ev.data["mcap_usd"] == 125000
    assert ev.data["liquidity_usd"] == 8000
    assert ev.data["tweet_count"] == 1
    assert "followers" not in ev.data

    events, recognized = parse_ws_message(fix["default_chain"])
    assert recognized is True and len(events) == 1
    assert events[0].mint == MINT2 and events[0].signal == "j7_tweet_ca"

    events, recognized = parse_ws_message(fix["solana_chain"])
    assert recognized is True and len(events) == 1 and events[0].mint == MINT

    events, recognized = parse_ws_message(fix["ethereum"])
    assert events == [] and recognized is True
    src = source(settings)
    assert src.on_ws(WS, fix["ethereum"]) == []
    assert not samples(settings).exists()


def test_vamp_trigger_is_launch():
    events, recognized = parse_ws_message(frame("vamp_trigger.json"))
    assert recognized is True and len(events) == 1
    ev = events[0]
    assert ev.signal == "j7_launch" and ev.source == "j7tracker" and ev.mint == MINT
    assert ev.data["author"] == "alice"
    assert ev.data["symbol"] == "CAT"
    assert ev.data["text"] == "Cat Coin CAT"
    assert ev.data["tweet_id"] == "vamp-1"
    assert "tweet_count" not in ev.data
    ev.ts = 1000
    market = MarketState()
    st = market.apply(ev)
    view = FeatureView(st, 1060, market)
    assert view.get("j7_tweeted") is True
    assert view.get("j7_tweet_count") is None
    assert view.get("j7_secs_since_first_tweet") == 60

    update, recognized = parse_ws_message(load("vamp_trigger.json")["update"])
    assert recognized is True and update[0].signal == "j7_launch" and update[0].mint == MINT2

    for key, raw in load("vamp_keys.json")["frames"].items():
        found, recognized = parse_ws_message(raw)
        assert recognized is True and len(found) == 1, key
        assert found[0].signal == "j7_launch" and found[0].mint == MINT


def test_feed_card_launch():
    fix = load("feed_card.json")
    for raw in (fix["frame"], fix["plural"]):
        events, recognized = parse_ws_message(raw)
        assert recognized is True and len(events) == 1
        assert events[0].signal == "j7_launch" and events[0].mint == MINT2


def test_noise_events_recognized_and_not_sampled(settings):
    src = source(settings)
    for name in ("noise_buy_success.json", "noise_deploy_success.json", "noise_connected_users.json"):
        raw = frame(name)
        assert parse_ws_message(raw) == ([], True)
        assert src.on_ws(WS, raw) == []
    assert not samples(settings).exists()


@pytest.mark.parametrize("name", ["ping.json", "pong.json"])
def test_engineio_ping_pong_not_sampled(settings, name):
    raw = frame(name)
    assert raw in {"2", "3"}
    assert parse_ws_message(raw) == ([], True)
    src = source(settings)
    assert src.on_ws(WS, raw) == []
    assert not samples(settings).exists()


def test_garbage_is_sampled(settings):
    raw = frame("garbage.json")
    assert parse_ws_message(raw) == ([], False)
    src = source(settings)
    assert src.on_ws(WS, raw) == []
    line = json.loads(samples(settings).read_text().splitlines()[0])
    assert line["kind"] == "ws" and line["url"] == WS and raw in line["payload"]


def test_unknown_event_is_sampled(settings):
    raw = frame("unknown_event.json")
    events, recognized = parse_ws_message(raw)
    assert events == [] and recognized is False
    src = source(settings)
    assert src.on_ws(WS, raw) == []
    body = samples(settings).read_text()
    assert "not_a_j7_event" in body and MINT in body


def test_bare_dict_tweet_and_unrecognized(settings):
    events, recognized = parse_ws_message(load("bare_tweet.json")["payload"])
    assert recognized is True and len(events) == 1
    assert events[0].mint == MINT and events[0].signal == "j7_tweet_ca"
    assert events[0].data["author"] == "alice" and events[0].data["is_reply"] is True
    bad = load("bare_unrecognized.json")["payload"]
    assert parse_ws_message(bad) == ([], False)
    src = source(settings)
    assert src.on_ws(WS, bad) == []
    assert "foo" in samples(settings).read_text()


def test_parse_never_raises_on_garbage_types():
    assert parse_ws_message(None) == ([], False)
    assert parse_ws_message(123) == ([], False)
    assert parse_ws_message(b"\xff") == ([], False)


def test_source_identity_and_lookalike(settings):
    assert Source.name == "j7tracker"
    assert Source.start_url == "https://j7tracker.io/?popout=social"
    assert Source.login_url == "https://j7tracker.io/"
    assert Source.ws_patterns == ["j7tracker.io"]
    assert any(p in "wss://dfw.j7tracker.io/socket.io/" for p in Source.ws_patterns)
    assert not any(p in LOOKALIKE for p in Source.ws_patterns)
    assert not any(p in "https://j7tracker.com/" for p in Source.ws_patterns)
    src = source(settings)
    assert src.name == "j7tracker"
    assert src.on_ws(LOOKALIKE, frame("tweet_valid.json")) == []
    assert not samples(settings).exists()
    found = sigs(src.on_ws(WS, frame("tweet_valid.json")))
    assert len(found) == 1 and found[0].mint == MINT
    assert not samples(settings).exists()


def test_features_after_apply():
    for name in SOCIAL:
        assert FEATURES[name].group == "social"
    stats = MintStats()
    first, _ = parse_ws_message(frame("tweet_first.json"), stats=stats)
    second, _ = parse_ws_message(frame("tweet_second.json"), stats=stats)
    first[0].ts = 1000
    second[0].ts = 1030
    market = MarketState()
    market.apply(first[0])
    st = market.apply(second[0])
    view = FeatureView(st, 1060, market)
    assert view.get("j7_tweeted") is True
    assert view.get("j7_tweet_count") == 2
    assert view.get("j7_max_followers") == 5000
    assert view.get("j7_secs_since_first_tweet") == 60

    assert market.get(MINT2) is None
    unseen = FeatureView(TokenState(mint=MINT2, first_seen_ts=0), 1060, market)
    for name in SOCIAL:
        assert unseen.get(name) is None

    bare = MarketState()
    missing = bare.apply(ExternalSignal(
        ts=1000, source="j7tracker", mint=MINT, signal="j7_tweet_ca", data={"author": "alice"}))
    missing_view = FeatureView(missing, 1060, bare)
    assert missing_view.get("j7_tweeted") is True
    assert missing_view.get("j7_tweet_count") is None
    assert missing_view.get("j7_max_followers") is None
    assert missing_view.get("j7_secs_since_first_tweet") == 60

    fallback = bare.apply(ExternalSignal(
        ts=1000, source="j7tracker", mint=MINT2, signal="j7_launch", data={"followers": 77}))
    fallback_view = FeatureView(fallback, 1060, bare)
    assert fallback_view.get("j7_tweeted") is True
    assert fallback_view.get("j7_tweet_count") is None
    assert fallback_view.get("j7_max_followers") == 77
    assert fallback_view.get("j7_secs_since_first_tweet") == 60
