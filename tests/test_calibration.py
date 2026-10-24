"""Paper fills learn from the live dry run: gaps measured at real prices move paper's fills."""

import random

from solmeme.config import FillsCfg
from solmeme.sim import calibration as cal
from solmeme.sim.fills import FillModel


def _tape(n_missed=12, n_bought=4, n_no_route=0, gap=0.4):
    t = [dict(side="missed", price=1.0 + gap, paper_price=1.0, why="slippage") for _ in range(n_missed)]
    t += [dict(side="buy", price=1.1, paper_price=1.0) for _ in range(n_bought)]
    t += [dict(side="missed", why="no route found") for _ in range(n_no_route)]
    t += [dict(side="sell", price=0.9, paper_price=1.0) for _ in range(n_bought)]
    return t


def test_the_correction_grows_with_the_sample_and_counts_no_route():
    small = cal.compute(_tape(2, 1), prior_n=30)
    big = cal.compute(_tape(120, 40, n_no_route=40), prior_n=30)
    assert 0 < small.weight < 0.1 < 0.8 < big.weight
    assert abs(big.no_route - 40 / 200) < 1e-9 and len(big.buy_gaps) == 160
    assert max(big.buy_gaps) == 0.4 and min(big.sell_gaps) == -0.1


def test_paper_buys_get_worse_and_miss_like_live(settings, tmp_path):
    c = cal.compute(_tape(300, 100), prior_n=0)                     # all evidence, no shrink: w = 1
    cfg = FillsCfg(fail_rate=0.0, sandwich_prob=0.0, slippage_tolerance_pct=25)

    class St:                                                        # a pump.fun curve token at 1e-6 SOL
        price_sol, migrated, pool, mcap_sol, amm = 1e-6, False, "pump", 30.0, None
        v_sol, v_tokens = 30.0, 30.0 / 1e-6

    plain, real = FillModel(cfg), FillModel(cfg, c)
    ok_plain = sum(plain.buy(St, 0.05, 1e-6, random.Random(i)).ok for i in range(200))
    fills = [real.buy(St, 0.05, 1e-6, random.Random(i)) for i in range(200)]
    assert ok_plain == 200
    missed = sum(not f.ok and f.reason == "slippage" for f in fills)
    assert 120 < missed < 180                                        # ~75% of attempts ran past the limit, as live did
    got = [f for f in fills if f.ok]
    assert got and all(f.price > plain.buy(St, 0.05, 1e-6, random.Random(0)).price for f in got)   # and paid more
    sold = real.sell(St, 1000.0, 1e-6, random.Random(1), force=True).sol
    assert sold < plain.sell(St, 1000.0, 1e-6, random.Random(1), force=True).sol                 # exits get less


def test_saved_and_loaded(settings):
    c = cal.compute(_tape(), prior_n=30)
    cal.save(settings.data_dir, c)
    back = cal.load(settings.data_dir)
    assert back.n == c.n and back.buy_gaps == c.buy_gaps and abs(back.weight - c.weight) < 1e-9
    assert cal.load(settings.data_dir / "nope") is None


def test_calibration_only_ever_makes_paper_fills_worse():
    import random as _r

    from solmeme.sim.calibration import Calibration

    c = Calibration(buy_gaps=[-0.5, -0.3, 0.2], sell_gaps=[0.4, -0.2], no_route=0.0, n=1000, weight=1.0, updated=0)
    rng = _r.Random(3)
    assert min(c.buy_gap(rng) for _ in range(300)) == 0.0          # a negative live gap never becomes a bargain
    assert max(c.sell_gap(rng) for _ in range(300)) == 0.0         # and a sell never gets a better price
