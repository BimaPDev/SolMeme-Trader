"""pump.fun Trending and Hot callouts → signals → per-coin callouts and features."""

from solmeme.core.events import ExternalSignal
from solmeme.features.registry import FeatureView
from solmeme.features.token_state import MarketState
from solmeme.sources.pumpfun_social import parse_callouts, parse_trending


def test_trending_and_callouts_become_features():
    tr = parse_trending({"entries": [{"m": "Lpump", "c": "solana:mainnet", "t": "LUMPY", "mc": 66116, "kol": 9, "tx5": 247},
                                     {"m": "0xabc", "c": "base:mainnet"}]})
    assert tr == [("Lpump", {"rank": 1, "symbol": "LUMPY", "mcap_usd": 66116.0, "kol": 9, "tx_5m": 247})]
    feed = {"coins": [{"coinMint": "Lpump", "symbol": "LUMPY", "chain": "solana", "recommendationRank": 1,
                       "position": {"userName": "Anglio", "walletAddress": "W1", "totalCallouts": 61, "pnlUsd": 688.6,
                                    "callout": {"calloutId": "c1", "calledOutAtMcap": 11859, "multiple": 7.3,
                                                "thesis": "new elon musk movie"}}},
                      {"coinMint": "Npump", "position": {"userName": "x"}}]}
    calls = parse_callouts(feed, {"W1": 3})
    assert len(calls) == 1 and calls[0][1]["caller_rank"] == 3 and calls[0][1]["thesis"] == "new elon musk movie"
    m = MarketState(); m.sol_usd = 100.0
    m.apply(ExternalSignal(ts=1.0, source="pump_trending", mint="Lpump", signal="pump_trending", data={**tr[0][1], "seen_ts": 1.0}))
    m.apply(ExternalSignal(ts=2.0, source="pump_callout", mint="Lpump", signal="pump_callout", data=calls[0][1]))
    st = m.get("Lpump"); st.set_price(2.0, 1.2e-6)                   # mcap 1,200 SOL = $120k
    fv = FeatureView(st, 3.0, m)
    assert fv.get("pump_trending_rank") == 1 and fv.get("pump_trending_kol") == 9
    assert fv.get("pump_callouts") == 1 and fv.get("pump_callout_leader") is True
    assert round(fv.get("pump_callout_mcap_x"), 1) == round(120000 / 11859, 1)
    assert FeatureView(st, 400.0, m).get("pump_trending_rank") is None      # off the board after 5 min


def test_pump_coin_flags_become_features():
    m = MarketState()
    m.apply(ExternalSignal(ts=1.0, source="pump_coin", mint="Fpump", signal="pump_coin",
                           data={"holder_reward": True, "cashback": False, "mayhem": False, "charity": False, "live": True}))
    fv = FeatureView(m.get("Fpump"), 2.0, m)
    assert fv.get("pump_holder_reward") is True and fv.get("pump_live") is True and fv.get("pump_mayhem") is False
