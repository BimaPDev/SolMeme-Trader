"""Narratives: tweets, j7tracker AI coin guesses and news become word indexes that launches are
matched against, and j7tracker frames that used to be dropped now feed them."""

import json

from solmeme.core.events import ExternalSignal, Headline, TokenLaunched
from solmeme.features.narrative import token_words, words
from solmeme.features.registry import FEATURES
from solmeme.features.token_state import MarketState
from solmeme.sources.j7tracker import headlines_from, parse_ws_message
from solmeme.sources.news import parse_reddit, parse_rss

A = "A" * 43
B = "B" * 43


def _f(name, st, now, m):
    return FEATURES[name].fn(st, now, m)


def _launch(ts, mint, name, symbol, uri=""):
    return TokenLaunched(ts=ts, source="pumpportal", mint=mint, name=name, symbol=symbol, pool="pump",
                         v_sol=30.0, v_tokens=1.073e9, uri=uri)


def test_words_skip_filler_and_keep_the_ticker():
    assert words("The OFFICIAL Trump coin on Solana!") == {"trump"}
    assert token_words("Michael Douglas", "DOUGLAS") == {"michael", "douglas"}
    assert "ai" not in token_words("AI dog", "AI")


def test_a_launch_matching_a_recent_tweet_and_news():
    m = MarketState()
    m.apply(Headline(ts=100.0, source="j7tracker", id="t1", author="elonmusk", followers=2e8, text="my dog Floki is the best"))
    m.apply(Headline(ts=110.0, source="gnews", id="n1", text="Floki the dog wins award"))
    st = m.apply(_launch(200.0, A, "Floki Dog", "FLOKIDOG"))
    assert _f("tweet_match_30m", st, 200.0, m) == 1 and _f("tweet_match_followers", st, 200.0, m) == 2e8
    assert _f("news_match_6h", st, 200.0, m) == 1
    assert _f("tweet_match_30m", st, 100.0 + 1801, m) == 0         # the tweet is old news after 30 min
    assert _f("news_match_6h", st, 99.0, m) == 0                   # never sees headlines from its future


def test_ai_ticker_match_and_crowded_themes():
    m = MarketState()
    m.apply(Headline(ts=50.0, source="j7ai", id="x:AWE", text="AI Wealth Effect", ticker="AWE", name="AI Wealth Effect"))
    st = m.apply(_launch(60.0, A, "Wealth thing", "AWE"))
    assert _f("ai_ticker_match", st, 60.0, m) is True
    other = m.apply(_launch(61.0, B, "Effect of wealth", "EOW"))
    assert _f("theme_launches_1h", other, 62.0, m) == 1 and _f("theme_launches_1h", st, 62.0, m) == 1
    assert m.narratives.themes(m, 62.0) == []                      # under 3 launches is not a theme yet


def test_j7_launch_and_tweeted_contract_address():
    m = MarketState()
    st = m.apply(_launch(1.0, A, "x", "X", uri="https://metadata.j7tracker.io/abc.json"))
    assert _f("j7_launched", st, 2.0, m) is True and _f("ca_tweeted", st, 2.0, m) is False
    m.apply(ExternalSignal(ts=3.0, source="j7tracker", mint=A, signal="j7_tweet_ca"))
    assert _f("ca_tweeted", st, 4.0, m) is True


def test_j7_frames_now_yield_tweets_and_ai_guesses():
    tweet = {"id": "99", "text": "gm frens, big news soon", "author": {"handle": "cz", "followersCount": 9000000}}
    frame = "42" + json.dumps(["tweet", tweet])
    assert parse_ws_message(frame) == ([], True)                   # no contract address: no signal, as before
    seen = set()
    evs = headlines_from(frame, seen)
    assert [e.kind for e in evs] == ["headline"] and evs[0].author == "cz" and evs[0].followers == 9e6
    assert headlines_from(frame, seen) == []                       # same tweet again
    ai = {"tweet_url": "https://twitter.com/axios/status/1", "prediction": "AI Wealth Effect⁠", "ticker": "AWE",
          "tweet_id": "1"}
    frame = "42" + json.dumps(["ai_suggestion", ai])
    assert parse_ws_message(frame) == ([], True)                   # recognized, so no longer sampled
    evs = headlines_from(frame)
    assert evs[0].source == "j7ai" and evs[0].ticker == "AWE" and evs[0].name == "AI Wealth Effect"
    assert parse_ws_message('42["ai_suggestion_update",{"tweet_id":"1","results":[]}]') == ([], True)


def test_news_feeds_parse():
    rss = """<rss xmlns:ht="https://trends.google.com/trending/rss"><channel>
      <item><title>michael douglas</title><link>https://trends.google.com/x</link>
        <ht:news_item><ht:news_item_title>Douglas says Kobe visited</ht:news_item_title></ht:news_item></item>
      <item><title>world series</title><link>https://trends.google.com/x</link></item></channel></rss>"""
    hs = parse_rss("gtrends", rss)
    assert [h.id for h in hs] == ["michael douglas", "world series"] and "Kobe" in hs[0].text
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>t3_abc</id><title>Cat learns to drive</title>
      <category term="aww" label="r/aww"/><link href="https://www.reddit.com/r/aww/x"/></entry></feed>"""
    [r] = parse_reddit(atom)
    assert r.author == "r/aww" and r.text == "Cat learns to drive" and r.url.endswith("/x")
    assert parse_rss("gnews", "<not xml") == []


def test_dashboard_views_read_the_live_market():
    from types import SimpleNamespace

    from solmeme.ui.narrative_view import build_narratives, build_token

    m = MarketState()
    m.apply(Headline(ts=10.0, source="j7tracker", id="t", author="a", followers=5e5, text="frog king rises"))
    m.apply(Headline(ts=11.0, source="gtrends", id="g", text="frog king"))
    for i, mint in enumerate((A, B, "C" * 43)):
        m.apply(_launch(20.0 + i, mint, "Frog King", f"FROG{i}"))
    m.apply(ExternalSignal(ts=25.0, source="j7tracker", mint=A, signal="j7_tweet_ca"))
    session = SimpleNamespace(engine=SimpleNamespace(market=m, now=30.0), whalebook=None, banlist=None)
    nar = build_narratives(session)
    assert {t["word"] for t in nar["themes"]} >= {"frog", "king"} and nar["themes"][0]["launches"] == 3
    assert nar["tweets"][0]["author"] == "a" and nar["news"][0]["source"] == "gtrends"
    card = build_token(session, A)
    assert card["features"]["tweet_match_30m"] == 1 and card["features"]["ca_tweeted"] is True
    assert any("contract address was tweeted" in f for f in card["red_flags"])
    assert card["story"] and build_token(session, "Z" * 43)["error"]


def test_copycats_point_back_to_the_original():
    m = MarketState()
    og = m.apply(_launch(100.0, A, "Moon Dog", "MDOG"))
    assert FEATURES["is_og"].fn(og, 100.0, m) is False and FEATURES["copies_after"].fn(og, 100.0, m) == 0
    copy = m.apply(_launch(160.0, B, "moon dog", "MOONDOG"))         # same name, different ticker
    m.apply(_launch(170.0, "C" * 43, "Other", "mdog"))               # same ticker, different name
    assert list(m.narratives.copied) == [A, A]                       # live: start price-tracking the OG
    assert FEATURES["is_og"].fn(og, 200.0, m) is True and FEATURES["copies_after"].fn(og, 200.0, m) >= 1
    assert FEATURES["og_rank"].fn(copy, 200.0, m) == 2 and FEATURES["is_og"].fn(copy, 200.0, m) is False
    assert FEATURES["copies_after"].fn(og, 150.0, m) == 0              # no peeking at later copies
    [c] = [c for c in m.narratives.clusters(m, 200.0) if c["og"] == A][:1] or [None]
    assert c is None or c["copies"] >= 2
    late = m.apply(_launch(100.0 + 7300, "D" * 43, "Moon Dog", "MDOG"))  # 2h later: a new cluster, its own OG
    assert FEATURES["og_rank"].fn(late, 7400.0, m) == 1


def test_the_dev_launch_bag_is_measured_and_blocked():
    from solmeme.config import load_settings

    m = MarketState()
    big = m.apply(TokenLaunched(ts=1.0, source="pumpportal", mint=A, name="x", symbol="X", pool="pump", v_sol=60.0,
                                v_tokens=5.4e8, initial_buy_sol=30.0, initial_buy_tokens=4.0e8))
    small = m.apply(TokenLaunched(ts=1.0, source="pumpportal", mint=B, name="y", symbol="Y", pool="pump", v_sol=31.0,
                                  v_tokens=1.04e9, initial_buy_sol=1.0, initial_buy_tokens=3.3e7))
    assert FEATURES["dev_launch_pct"].fn(big, 2.0, m) == 40.0 and round(FEATURES["dev_launch_pct"].fn(small, 2.0, m), 1) == 3.3
    rules = {r["id"]: r for r in load_settings().prefilter.none}
    assert rules["dev_launch_bag"]["f"] == "dev_launch_pct" and rules["dev_launch_bag"]["v"] == 30
