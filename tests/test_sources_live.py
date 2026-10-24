"""Real-time and backup price sources: SolanaStreaming swap ticks and GeckoTerminal snapshots,
and which price wins when they disagree in time."""

from solmeme.core.events import Enrichment, ExternalSignal, SolPrice
from solmeme.features.registry import FEATURES
from solmeme.features.token_state import MarketState
from solmeme.sources.gecko import parse_tokens, parse_trending
from solmeme.sources.solstream import WSOL, parse_swap

MINT = "BwyMeGp9kqCQ7zYn1mEj5REkMFKZhRRHfCEJ8eqcmoon"
WHALE = "5S2fWgsbzqTTGVkUUrk83iH17h9T8C5ojYxhrDUWbg6L"


def _swap(mint=MINT, quote=WSOL, price="4.665456971e-06", side="buy"):
    return {"slot": 1, "signature": "sig1", "blockTime": 1,
            "swap": {"ammAccount": "amm", "baseTokenMint": mint, "quoteTokenMint": quote, "walletAccount": WHALE,
                     "quotePrice": price, "usdValue": 466.04, "baseAmount": "398674773250", "swapType": side}}


def test_a_swap_on_a_watched_token_is_a_price_tick():
    evs = parse_swap(_swap(), 10.0, {MINT}, set(), None)
    assert len(evs) == 1 and evs[0].source == "solstream" and evs[0].mint == MINT
    assert abs(evs[0].data["price_sol"] - 4.665456971e-06) < 1e-15 and evs[0].data["side"] == "buy"


def test_a_tracked_wallet_swap_is_a_wallet_trade_even_off_the_watchlist():
    evs = parse_swap(_swap(), 10.0, set(), {WHALE}, None)
    assert [e.kind for e in evs] == ["wallet_trade"] and evs[0].wallet == WHALE and evs[0].usd == 466.04


def test_unknown_quotes_and_bad_rows_are_skipped():
    assert parse_swap(_swap(quote="SomeOtherMint111111111111111111111111111111"), 1.0, {MINT}, set(), None) == []
    assert parse_swap(_swap(mint=WSOL), 1.0, {WSOL}, set(), None) == []
    assert parse_swap({"swap": {"baseTokenMint": MINT, "swapType": "swap"}}, 1.0, {MINT}, set(), None) == []


def test_streamed_price_beats_older_snapshots_then_lets_them_back():
    m = MarketState()
    m.apply(SolPrice(ts=0.0, usd=100.0))
    m.apply(Enrichment(ts=10.0, source="solstream", mint=MINT, data={"price_sol": 2e-6}))
    st = m.apply(Enrichment(ts=12.0, source="dexscreener", mint=MINT, data={"price_sol": 1e-6, "dex_id": "pumpswap"}))
    assert st.price_sol == 2e-6                                  # the 5 s old Dex snapshot doesn't step it back
    st = m.apply(Enrichment(ts=50.0, source="dexscreener", mint=MINT, data={"price_sol": 1e-6, "dex_id": "pumpswap"}))
    assert st.price_sol == 1e-6                                  # stream went quiet: Dex prices again


def test_gecko_prices_only_fill_in_while_dex_is_stale():
    m = MarketState()
    m.apply(SolPrice(ts=0.0, usd=100.0))
    m.apply(Enrichment(ts=10.0, source="dexscreener", mint=MINT, data={"price_sol": 1e-6, "dex_id": "pumpswap"}))
    st = m.apply(Enrichment(ts=15.0, source="gecko", mint=MINT, data={"price_usd": 3e-4}))
    assert st.price_sol == 1e-6
    st = m.apply(Enrichment(ts=60.0, source="gecko", mint=MINT, data={"price_usd": 3e-4}))
    assert abs(st.price_sol - 3e-6) < 1e-15


def test_gecko_trending_parses_and_becomes_a_feature():
    body = {"data": [{"attributes": {"base_token_price_native_currency": "0.0000053", "base_token_price_usd": "0.00058",
                                     "market_cap_usd": "555633.7", "transactions": {"m5": {"buyers": 28, "sellers": 19}},
                                     "price_change_percentage": {"m5": "0.3"}},
                      "relationships": {"base_token": {"data": {"id": f"solana_{MINT}"}},
                                        "quote_token": {"data": {"id": f"solana_{WSOL}"}},
                                        "dex": {"data": {"id": "pump-fun"}}}}]}
    [(mint, d)] = parse_trending(body)
    assert mint == MINT and d["price_sol"] == 5.3e-6 and d["buyers_m5"] == 28 and d["dex"] == "pump-fun"
    m = MarketState()
    st = m.apply(ExternalSignal(ts=5.0, source="gecko", mint=MINT, signal="gecko_trending", data=d))
    assert FEATURES["gecko_trending"].fn(st, 6.0, m) is True
    tok = {"data": [{"attributes": {"address": MINT, "symbol": "MOON", "price_usd": "0.0005", "fdv_usd": "500000"}}]}
    assert parse_tokens(tok) == [(MINT, {"symbol": "MOON", "price_usd": 0.0005, "mcap_usd": 500000.0})]


def test_volume_to_market_cap_ratio():
    m = MarketState()
    m.apply(SolPrice(ts=0.0, usd=100.0))
    st = m.apply(Enrichment(ts=1.0, source="dexscreener", mint=MINT,
                            data={"price_sol": 1e-6, "dex_id": "pumpswap", "volume_h1_usd": 300000.0}))
    assert abs(FEATURES["vol_mcap_1h"].fn(st, 2.0, m) - 3.0) < 1e-9     # $100k mcap, $300k volume
