import math
import random

import pytest

from solmeme.config import FillsCfg
from solmeme.features.token_state import PUMP_K, TokenState
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
