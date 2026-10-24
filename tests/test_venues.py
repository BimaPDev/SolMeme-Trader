"""Launchpad venues: GMGN-only tokens learn their curve (Meteora DBC is not pump.fun), and each
curve pays its own fee in paper fills."""

from solmeme.config import FillsCfg
from solmeme.core.events import Enrichment, TokenLaunched
from solmeme.features.token_state import MarketState
from solmeme.sim.fills import FillModel

MINT = "DbcMint11111111111111111111111111111111111"


def _gmgn(ts, launchpad, mint=MINT):
    return Enrichment(ts=ts, source="gmgn_trench", mint=mint,
                      data={"launchpad": launchpad, "price_usd": 0.00002, "created_ts": ts - 30})


def test_gmgn_launchpad_sets_the_pool():
    m = MarketState()
    st = m.apply(_gmgn(100.0, "meteora_virtual_curve"))
    assert st.pool == "meteoradbc" and st.pool_src == "gmgn"
    assert m.apply(_gmgn(101.0, "bags", "BagsMint1111111111111111111111111111111111")).pool == "meteoradbc"
    assert m.apply(_gmgn(102.0, "pump_mayhem", "MayhemMint11111111111111111111111111111111")).pool == "pump"


def test_a_pumpportal_launch_outranks_gmgn():
    m = MarketState()
    m.apply(TokenLaunched(ts=99.0, source="pumpportal", mint=MINT, pool="pump", v_sol=30.0, v_tokens=1.073e9))
    st = m.apply(_gmgn(100.0, "meteora_virtual_curve"))
    assert st.pool == "pump" and st.pool_src == "launch"


def test_each_curve_pays_its_own_fee():
    cfg = FillsCfg()
    assert cfg.curve_fee("pump") == 1.25 and cfg.curve_fee("meteoradbc") == 2.0
    cfg2 = FillsCfg(curve_fee_by_pool={"meteoradbc": 1.0})
    assert cfg2.curve_fee("meteoradbc") == 1.0
    m = MarketState()
    a = m.apply(TokenLaunched(ts=1.0, source="pumpportal", mint="A" * 43, pool="pump", v_sol=30.0, v_tokens=1.073e9))
    b = m.apply(TokenLaunched(ts=1.0, source="pumpportal", mint="B" * 43, pool="meteoradbc", v_sol=30.0, v_tokens=1.073e9))
    fm = FillModel(cfg)
    assert fm.quote_buy(a, 0.1)[0] > fm.quote_buy(b, 0.1)[0]    # same curve, higher fee → fewer tokens
