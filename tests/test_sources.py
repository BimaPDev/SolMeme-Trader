"""Parsers on payload shapes captured from the live services."""

from solmeme.sources.dexscreener import normalize_pair
from solmeme.sources.gmgn import normalize_activity, normalize_trench
from solmeme.sources.pumpportal import parse_message
from solmeme.sources.watchlist import Watchlist
from solmeme.core.events import ExternalSignal, WalletTrade

CREATE = {"signature": "sig1", "mint": "Mint111pump", "traderPublicKey": "Dev111", "txType": "create",
          "initialBuy": 35_000_000.0, "solAmount": 1.0, "bondingCurveKey": "BC1",
          "vTokensInBondingCurve": 1_038_000_000.0, "vSolInBondingCurve": 31.0, "marketCapSol": 29.86,
          "name": "Test", "symbol": "TST", "uri": "https://x", "pool": "pump", "is_mayhem_mode": False}
BUY = {"signature": "sig2", "mint": "Mint111pump", "traderPublicKey": "W1", "txType": "buy",
       "tokenAmount": 10_000_000.0, "solAmount": 0.3, "newTokenBalance": 10_000_000.0, "bondingCurveKey": "BC1",
       "vTokensInBondingCurve": 1_028_000_000.0, "vSolInBondingCurve": 31.3, "marketCapSol": 30.4, "pool": "pump"}
AMM = {"signature": "sig3", "mint": "Mint111pump", "traderPublicKey": "W2", "txType": "sell",
       "tokenAmount": 1_000_000.0, "solAmount": 0.5, "pool": "pump-amm"}


def test_pumpportal_create_trade_migrate():
    (launch,) = parse_message(CREATE, 1.0, set())
    assert launch.kind == "launch" and launch.creator == "Dev111" and launch.v_sol == 31.0
    evs = parse_message(BUY, 2.0, {"W1"})
    assert [e.kind for e in evs] == ["curve_trade", "wallet_trade"]
    assert evs[0].new_balance == 10_000_000.0
    (pool,) = parse_message(AMM, 3.0, set())
    assert pool.kind == "pool_trade" and pool.side == "sell"
    (mig,) = parse_message({"signature": "s", "mint": "Mint111pump", "txType": "migrate", "pool": "pump-amm"}, 4.0, set())
    assert mig.kind == "migration"
    assert parse_message({"message": "Successfully subscribed"}, 5.0, set()) == []


def test_gmgn_trench_keeps_risky_tokens():
    """No ingest-time filtering: a 0.9 rug ratio token must still be recorded (strategies filter)."""
    mint, d = normalize_trench({"address": "M1", "symbol": "X", "rug_ratio": 0.9, "top_10_holder_rate": 0.42,
                                "holder_count": 77, "usd_market_cap": 12345}, "new_creation")
    assert mint == "M1" and d["rug_ratio"] == 0.9 and d["top10_pct"] == 42 and d["holders"] == 77


def test_gmgn_activity_normalization():
    ev = normalize_activity({"maker": "K1", "event_type": "buy", "token": {"address": "M1", "symbol": "X"},
                             "cost_usd": "250", "transaction_hash": "tx"}, "kol")
    assert isinstance(ev, WalletTrade) and ev.side == "buy" and ev.usd == 250 and ev.label == "kol"


def test_dexscreener_pair():
    mint, d = normalize_pair({"chainId": "solana", "dexId": "pumpfun", "pairAddress": "P",
                              "baseToken": {"address": "M1", "symbol": "X"}, "quoteToken": {"symbol": "SOL"},
                              "priceNative": "0.0000001", "priceUsd": "0.000015",
                              "txns": {"m5": {"buys": 12, "sells": 4}}, "volume": {"m5": 900},
                              "priceChange": {"m5": 8.5}, "liquidity": {"usd": 5000, "quote": 20}})
    assert mint == "M1" and d["price_sol"] == 1e-7 and d["buys_m5"] == 12 and d["liquidity_sol"] == 40


def test_watchlist_priority():
    w = Watchlist(ttl_s=100, launch_window_s=50)
    w.touch("launch", "launch", 0)
    w.observe(ExternalSignal(ts=1, mint="trench", signal="gmgn_new"))
    w.observe(WalletTrade(ts=2, wallet="k", mint="kol", side="buy"))
    w.set_pinned({"held"})
    assert w.priority_list(10) == ["held", "kol", "trench", "launch"]
    assert "launch" not in w.priority_list(60)          # launch window expired
