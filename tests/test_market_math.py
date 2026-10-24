import math
import random

import pytest

from solmeme.config import FillsCfg
from solmeme.core.events import CurveTrade, Enrichment, TokenLaunched
from solmeme.features.token_state import PUMP_K, MarketState, TokenState
from solmeme.sim import curve
from solmeme.sim.amm import Pool
from solmeme.sim.fills import FillModel, order_rng


def test_curve_buy_known_value():
    # 1 SOL net into a fresh curve: 1.073e9 - 32.19e9/31 = 34,612,903.2 tokens
    out, v_sol, v_tok = curve.buy_out(curve.INITIAL_V_SOL, curve.INITIAL_V_TOKENS, 1.0, fee_pct=0)
    assert out == pytest.approx(34_612_903.2258, rel=1e-9)
    assert v_sol * v_tok == pytest.approx(PUMP_K)


def test_curve_round_trip_loses_only_fees():
    fee = 1.25
    tok, vs, vt = curve.buy_out(curve.INITIAL_V_SOL, curve.INITIAL_V_TOKENS, 2.0, fee)
    sol, _, _ = curve.sell_out(vs, vt, tok, fee)
    assert sol == pytest.approx(2.0 * (1 - fee / 100) ** 2, rel=1e-9)


def test_bonding_pct_bounds():
    assert curve.bonding_pct(curve.INITIAL_V_TOKENS) == 0
    assert curve.bonding_pct(curve.INITIAL_V_TOKENS - curve.INITIAL_REAL_TOKENS) == pytest.approx(100)


def test_price_pins_reserves_on_pump_curve():
    st = TokenState(mint="m", first_seen_ts=0)
    p = curve.price(60.0, PUMP_K / 60.0)
    st.set_price(1.0, p)
    assert st.v_sol == pytest.approx(60.0)
    assert st.v_tokens == pytest.approx(PUMP_K / 60.0)


def test_launch_reserves_follow_snapshot_prices():
    # Free tier: we see the launch, then only Dex snapshots. The fill curve and bonding_pct
    # must move with those prices instead of staying pinned at the launch reserves.
    m = MarketState()
    mint = "GFG2cM94DhGBDzrU2JSutv7zUYTMZozksTp7suA5pump"
    m.apply(TokenLaunched(ts=0.0, source="pumpportal", mint=mint, v_sol=30.5, v_tokens=PUMP_K / 30.5))
    st = m.apply(Enrichment(ts=60.0, source="dexscreener", mint=mint, data={"price_sol": 1.5e-7, "dex_id": "pumpfun"}))
    assert st.v_sol / st.v_tokens == pytest.approx(1.5e-7)
    assert curve.bonding_pct(st.v_tokens) > 30
    fm = FillModel(FillsCfg(fail_rate=0.0, sandwich_prob=0.0))
    f = fm.buy(st, 0.1, ref_price=1.5e-7, rng=random.Random(0))
    assert f.ok and f.price == pytest.approx(1.5e-7, rel=0.05)


def test_trade_stream_reserves_stay_exact():
    m = MarketState()
    mint = "GFG2cM94DhGBDzrU2JSutv7zUYTMZozksTp7suA5pump"
    m.apply(TokenLaunched(ts=0.0, source="pumpportal", mint=mint, v_sol=30.5, v_tokens=PUMP_K / 30.5))
    m.apply(CurveTrade(ts=1.0, source="pumpportal", mint=mint, trader="w", side="buy", sol=1.0, tokens=3e7,
                       v_sol=40.0, v_tokens=PUMP_K / 40.0))
    st = m.apply(Enrichment(ts=2.0, source="dexscreener", mint=mint, data={"price_sol": 9e-7}))
    assert st.v_sol == 40.0                    # the stream is fresher than a snapshot


def test_amm_seed_keeps_price():
    pool = Pool.seed_from_price(4e-7)
    assert pool.price == pytest.approx(4e-7)
    out = pool.buy_out(1.0, 0.3)
    assert 0 < out < 1.0 / 4e-7


def _state(price=1e-7):
    st = TokenState(mint="m", first_seen_ts=0)
    st.set_price(0.0, price)
    return st


def test_buy_fails_when_price_ran_past_tolerance():
    cfg = FillsCfg(fail_rate=0.0, sandwich_prob=0.0, slippage_tolerance_pct=10)
    fm = FillModel(cfg)
    st = _state(1.2e-7)                       # landed 20% above the 1e-7 signal price
    f = fm.buy(st, 0.1, ref_price=1e-7, rng=random.Random(0))
    assert not f.ok and f.reason == "slippage"
    assert f.cost_sol == cfg.priority_fee_sol  # failed tx still pays the priority fee


def test_buy_costs_and_effective_price():
    cfg = FillsCfg(fail_rate=0.0, sandwich_prob=0.0)
    fm = FillModel(cfg)
    st = _state(1e-7)
    f = fm.buy(st, 0.5, ref_price=1e-7, rng=random.Random(0))
    assert f.ok
    assert f.price > 1e-7 * (1 + cfg.curve_fee_pct / 100)   # fee + price impact
    assert f.cost_sol == pytest.approx(cfg.priority_fee_sol + cfg.jito_tip_sol + 0.5 * cfg.platform_fee_pct / 100
                                       + cfg.ata_rent_sol)


def test_order_rng_is_deterministic_and_isolated():
    a = order_rng(7, "s@v", "mint", 3).random()
    assert a == order_rng(7, "s@v", "mint", 3).random()
    assert a != order_rng(7, "other@v", "mint", 3).random()


def test_amm_fee_tiers():
    cfg = FillsCfg()
    assert cfg.amm_fee_pct(100) == 1.25
    assert cfg.amm_fee_pct(1e7) == 0.30
    assert not math.isnan(cfg.amm_fee_pct(None))


def test_stale_dex_price_does_not_fake_a_dip():
    # Dex can show a curve token's launch price for a minute while GMGN sees it triple. That stale
    # print must not become the price: it looked like a 60% dip and paper bought it at a price
    # nobody could get.
    m = MarketState()
    m.sol_usd = 100.0
    mint = "TAVUSmintxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxpump"
    m.apply(Enrichment(ts=0.0, source="gmgn_trenches", mint=mint, data={"price_usd": 9e-6}))
    st = m.apply(Enrichment(ts=1.0, source="dexscreener", mint=mint, data={"price_sol": 3e-8, "dex_id": "pumpfun"}))
    assert st.price_sol == pytest.approx(9e-8)
    st = m.apply(Enrichment(ts=2.0, source="gecko", mint=mint, data={"price_sol": 3e-8}))
    assert st.price_sol == pytest.approx(9e-8)
    # once GMGN goes quiet, Dex fills in again
    st = m.apply(Enrichment(ts=40.0, source="dexscreener", mint=mint, data={"price_sol": 8e-8, "dex_id": "pumpfun"}))
    assert st.price_sol == pytest.approx(8e-8)


def test_whale_buys_count_pump_feed_trades_too():
    from solmeme.core.events import WalletTrade
    from solmeme.features.registry import FeatureView

    m = MarketState()
    m.whales = {"W1", "W2", "W3"}
    mint = "Sosi" + "1" * 36 + "pump"
    m.apply(WalletTrade(ts=1.0, source="gmgn_tracked", wallet="W1", mint=mint, side="buy", sol=1.0))
    for i, w in enumerate(("W2", "W3", "Nobody")):
        m.apply(CurveTrade(ts=2.0 + i, source="pumpfun", mint=mint, trader=w, side="buy", sol=0.5, tokens=4e6,
                           v_sol=32.0, v_tokens=PUMP_K / 32.0))
    m.apply(CurveTrade(ts=6.0, source="pumpfun", mint=mint, trader="W2", side="sell", sol=0.2, tokens=1e6,
                       v_sol=31.0, v_tokens=PUMP_K / 31.0))
    fv = FeatureView(m.get(mint), 10.0, m)
    assert fv.get("whale_buys") == 3 and fv.get("whale_sells") == 1


def test_a_late_copy_trade_cluster_does_not_taint_a_runner():
    from solmeme.core.events import WalletTrade

    m = MarketState()
    m.banned = {"Clu1", "Clu2", "Dev1"}
    m.ban_roles = {"Clu1": "cluster", "Clu2": "cluster", "Dev1": "operation"}
    a, b = "Run" + "1" * 37 + "pump", "Snp" + "1" * 37 + "pump"
    for mint in (a, b):
        m.apply(TokenLaunched(ts=0.0, source="pumpportal", mint=mint, v_sol=30.5, v_tokens=PUMP_K / 30.5))
    m.apply(WalletTrade(ts=450.0, source="gmgn_smartmoney", wallet="Clu1", mint=a, side="buy"))   # +7.5 min: late
    m.apply(WalletTrade(ts=60.0, source="gmgn_smartmoney", wallet="Clu2", mint=b, side="buy"))    # +1 min: sniping
    assert not m.get(a).banned_hits and m.get(b).banned_hits == {"Clu2"}
    m.apply(WalletTrade(ts=900.0, source="gmgn_smartmoney", wallet="Dev1", mint=a, side="buy"))   # operators always count
    assert m.get(a).banned_hits == {"Dev1"}


def test_no_mev_tip_makes_paper_sandwiches_likelier_and_costlier():
    base = dict(fail_rate=0.0, sandwich_prob=0.0, sandwich_prob_unprotected=1.0, sandwich_extra_unprotected_pct=6.0,
                slippage_tolerance_pct=50)
    protected = FillModel(FillsCfg(jito_tip_sol=0.001, **base)).buy(_state(1e-7), 0.1, ref_price=1e-7, rng=random.Random(1))
    bare = FillModel(FillsCfg(jito_tip_sol=0.0, **base)).buy(_state(1e-7), 0.1, ref_price=1e-7, rng=random.Random(1))
    assert protected.ok and not protected.meta.get("sandwiched")        # with the tip: the protected rate (0 here)
    assert bare.ok and bare.meta.get("sandwiched") and bare.price == pytest.approx(protected.price * 1.06)


def test_a_stale_trade_stream_does_not_give_paper_a_fake_cheap_fill():
    # Oct 10, IMPOSTINU: the last pump.fun feed trade pinned the curve at 56 SOL mcap, GMGN then said 95,
    # the strategy decided at 95 and paper filled at 56 (+31.6% on paper, -23.6% live).
    from solmeme.core.events import CurveTrade
    from solmeme.features.token_state import MarketState

    m = MarketState()
    mint = "Stale" + "1" * 35 + "pump"
    v_sol = math.sqrt(PUMP_K * 5.6e-8)
    m.apply(CurveTrade(ts=0.0, source="pumpfun", mint=mint, trader="A", side="buy", sol=0.1, tokens=1e6,
                       v_sol=v_sol, v_tokens=PUMP_K / v_sol))
    st = m.get(mint)
    st.pool = "pump"
    fm = FillModel(FillsCfg(fail_rate=0.0, sandwich_prob=0.0, slippage_tolerance_pct=50))
    st.set_price(5.0, 9.5e-8)                          # 5 s later: the stream is still live, its reserves stand
    fresh = fm.buy(st, 0.05, ref_price=9.5e-8, rng=random.Random(0))
    assert fresh.price < 7e-8
    st.set_price(60.0, 9.5e-8)                         # a minute with no stream trade: follow the decided price
    stale = fm.buy(st, 0.05, ref_price=9.5e-8, rng=random.Random(0))
    assert 9.5e-8 < stale.price < 9.5e-8 * 1.05       # the decided price + fee and impact, not the frozen 5.6e-8


def test_holder_balances_add_up_from_trades_when_the_feed_sends_no_balance():
    """pump.fun's feed has no new-balance field (PumpPortal had): balances come from the trades themselves."""
    from solmeme.core.events import CurveTrade
    from solmeme.features.token_state import MarketState

    m = MarketState()
    mint = "Bal" + "1" * 37 + "pump"
    v = math.sqrt(PUMP_K * 3e-8)
    kw = dict(source="pumpfun", mint=mint, v_sol=v, v_tokens=PUMP_K / v)
    for ts, who, side, tok in ((1, "A", "buy", 3e7), (2, "B", "buy", 1e7), (3, "A", "sell", 1e7), (4, "B", "sell", 2e7)):
        m.apply(CurveTrade(ts=float(ts), trader=who, side=side, sol=0.1, tokens=tok, **kw))
    st = m.get(mint)
    assert st.balances == {"A": 2e7}                              # B sold more than we saw it buy: gone, not negative
    m.apply(CurveTrade(ts=5.0, trader="C", side="buy", sol=0.1, tokens=5e6, new_balance=9e6, **kw))
    assert st.balances["C"] == 9e6                               # a feed that does send the balance still wins
