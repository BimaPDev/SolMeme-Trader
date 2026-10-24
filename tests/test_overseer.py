"""The Overseer: the last check before a live buy."""

from solmeme.config import OverseerCfg
from solmeme.sim.overseer import Overseer


def test_each_guard_blocks_and_clears(tmp_path):
    ages = {"pumpfun": 5.0}
    o = Overseer(tmp_path, OverseerCfg(), health_fn=lambda: ages)
    item = {"key": "kol_follow@x", "mint": "M", "symbol": "X"}
    assert o.check(item, 1.0, 0.2, 1.0, []) is None
    ages["pumpfun"] = 500.0
    assert "data feed down" in o.check(item, 1.0, 0.2, 1.0, []) and "data" in o.status()["paused"]
    ages["pumpfun"] = 5.0
    assert "24 h" in o.check(item, 0.75, 0.0, 1.0, [])                         # -25% today
    assert "in positions" in o.check(item, 1.0, 0.8, 1.0, [])                   # 80% exposure
    lost = [{"key": "council_pick@c", "pnl_sol": -0.01}] * 4
    pick = {"key": "council_pick@c", "mint": "P"}
    assert "council picks paused" in o.check(pick, 1.0, 0.0, 1.0, lost)
    assert o.check(item, 1.0, 0.0, 1.0, lost) is None                         # other strategies still buy
    assert o.status()["paused"] == {} and len(o.status()["log"]) == 4
    o2 = Overseer(tmp_path, OverseerCfg()); o2.load()
    assert len(o2.log) == 4                                                     # kept across restarts
