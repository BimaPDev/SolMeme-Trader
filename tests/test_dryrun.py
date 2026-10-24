"""The live dry run mirrors champion fills at real-quote prices, fails the way real transactions fail,
and keeps its own wallet across restarts."""

import asyncio

from solmeme.core.store import Store
from solmeme.sim.dryrun import LAMPORTS, SOL_MINT, DryRun, Quote, QuoteError

MINT = "Coin111111111111111111111111111111111111pump"


class FakeJupiter:
    """Prices a pump.fun-style token at `price` SOL per token, with `impact` % worse fills."""

    def __init__(self, price=1e-6, impact=2.0):
        self.price, self.impact, self.down, self.calls = price, impact, set(), []

    async def quote(self, in_mint, out_mint, amount):
        self.calls.append((in_mint, out_mint, amount))
        if (in_mint, out_mint) in self.down or "all" in self.down:
            raise QuoteError("no route found")
        k = 1 - self.impact / 100
        if in_mint == SOL_MINT:                       # buy: SOL lamports in, raw tokens (6 decimals) out
            return Quote(int(amount / LAMPORTS / self.price * k * 1e6), self.impact, ["Pump.fun"], 150)
        return Quote(int(amount / 1e6 * self.price * k * LAMPORTS), self.impact, ["Pump.fun"], 150)


async def six(_mint):
    return 6


def _dry(settings, jup, store=None, now=[1000.0]):
    settings.dryrun.enabled = True
    settings.dryrun.start_usd = 50
    settings.portfolio.position_pct = 15                    # as in config/settings.yaml
    d = DryRun(settings, store, quote_fn=jup.quote, decimals_fn=six, clock=lambda: now[0])
    d.start_fresh(sol_usd=100.0, paper_equity_sol=4.0)
    return d


def _buy(price=1e-6, ref=1e-6):
    return dict(side="buy", key="holder_dip@v1", shared=True, mint=MINT, symbol="COIN", price=price, ref_price=ref)


def _sell(frac=1.0, price=2.6e-6, pnl_pct=150.0):
    return dict(side="sell", key="holder_dip@v1", shared=True, mint=MINT, symbol="COIN", price=price, frac=frac,
                why=["tp1"], pnl_pct=pnl_pct, trade_id="t1")


def test_a_round_trip_is_priced_from_real_quotes_and_sized_like_a_50_dollar_wallet(settings):
    store = Store(settings.db_path)
    jup = FakeJupiter()
    d = _dry(settings, jup, store)
    assert abs(d.start_sol - 0.5) < 1e-9                     # $50 at $100/SOL
    asyncio.run(d.buy(_buy()))
    pos = d.positions[("holder_dip@v1", MINT)]
    spent = jup.calls[0][2] / LAMPORTS
    assert abs(spent - 0.5 * settings.portfolio.position_pct / 100) < 1e-6    # 15% of its own $50, not the paper wallet
    assert pos.entry_price > 1e-6 and abs(pos.buy_impact_pct - 2.0) < 1e-9   # our own price impact is paid
    jup.price = 2.6e-6
    asyncio.run(d.sell(_sell()))
    assert not d.positions
    t = store.dryrun_trades()[0]
    assert t["status"] == "closed" and t["paper_pnl_pct"] == 150.0
    assert 0 < t["pnl_pct"] < 150.0                          # real fills: impact both ways, fees, rent back
    assert d.equity() > d.start_sol and d.counts["buys"] == 1 and d.counts["sells"] == 1


def test_no_route_and_slippage_fail_like_real_transactions(settings):
    jup = FakeJupiter()
    d = _dry(settings, jup)
    jup.down.add((SOL_MINT, MINT))
    asyncio.run(d.buy(_buy()))
    assert not d.positions and d.counts["no_route"] == 1 and d.cash < d.start_sol    # the attempt still paid a fee
    jup.down.clear()
    jup.impact = 40.0                                        # price ran away past the 25% limit
    asyncio.run(d.buy(_buy()))
    assert not d.positions and d.counts["slippage"] == 1
    assert [t["status"] for t in d.closed] == ["missed", "missed"]


def test_an_unsellable_coin_is_retried_then_counted_as_stuck(settings):
    jup = FakeJupiter()
    d = _dry(settings, jup)
    settings.dryrun.sell_retries = 2
    asyncio.run(d.buy(_buy()))
    jup.down.add((MINT, SOL_MINT))                           # rugged: nobody buys it
    asyncio.run(d.sell(_sell()))
    assert d.positions and d.counts["sell_retries"] == 1
    for _ in range(3):
        asyncio.run(d.retry_stuck_sells())
    assert not d.positions and d.counts["stuck"] == 1 and d.closed[-1]["status"] == "stuck"
    assert d.closed[-1]["pnl_sol"] < -0.07                   # the whole stake is lost


def test_partial_sells_and_restarts(settings):
    jup = FakeJupiter()
    d = _dry(settings, jup)
    asyncio.run(d.buy(_buy()))
    raw = d.positions[("holder_dip@v1", MINT)].raw
    asyncio.run(d.sell(_sell(frac=0.5)))
    assert d.positions[("holder_dip@v1", MINT)].raw == raw - int(raw * 0.5)
    again = DryRun(settings, None, quote_fn=jup.quote, decimals_fn=six)
    assert again.load() and again.positions.keys() == d.positions.keys() and abs(again.cash - d.cash) < 1e-12
    ignored = dict(_buy(), shared=False)                     # swarm agents' fills are not mirrored
    again.on_fill(ignored)
    again.on_fill(dict(_buy(), side="missed"))
    assert again.queue.empty()


def test_the_view_compares_with_the_paper_wallet(settings):
    jup = FakeJupiter()
    d = _dry(settings, jup)
    asyncio.run(d.buy(_buy()))
    jup.price = 2.6e-6
    asyncio.run(d.sell(_sell()))
    v = d.view(sol_usd=100.0, paper_equity_sol=5.0)
    assert v["paper_pnl_pct"] == 25.0                        # paper wallet 4 → 5 SOL since the dry run started
    assert v["trades"] == 1 and v["won"] == 1 and v["avg_gap_pct"] < 0   # real did worse than paper's +150%
    assert abs(v["equity_usd"] - v["equity_sol"] * 100) < 1e-9


def test_the_engine_hands_champion_fills_to_its_listeners(settings, specs):
    from solmeme.sim.engine import Engine

    eng = Engine([(specs["holder_dip"], "champion")], settings, shared_wallet=True)
    got = []
    eng.fill_listeners.append(got.append)
    eng.fill_listeners.append(lambda item: 1 / 0)           # a broken listener never breaks trading
    r = eng.runners[0]
    eng._tape(r, "buy", MINT, "COIN", 10.0, sol=0.07, price=1e-6, tokens=7e4, ref_price=1e-6)
    assert got and got[0]["shared"] is True and got[0]["ref_price"] == 1e-6 and got[0]["side"] == "buy"
    eng.journal.persist_after = 20.0                        # warm-start replay: not new trades, nobody hears them
    eng._tape(r, "sell", MINT, "COIN", 15.0, frac=1.0)
    assert len(got) == 1


def test_history_summary_and_the_ready_for_real_money_checks(settings):
    jup = FakeJupiter()
    now = [1000.0]
    d = _dry(settings, jup, now=now)
    for i in range(8):                                       # 8 small winners over 4 days
        m = f"Coin{i}" + "1" * 34 + "pump"
        jup.price = 1e-6
        asyncio.run(d.buy(dict(_buy(), mint=m)))
        now[0] += 43200
        jup.price = 1.5e-6
        asyncio.run(d.sell(dict(_sell(), mint=m)))
    assert len(d.series) >= 16 and d.series[-1][1] == d.equity()
    s = d.summary(sol_usd=100.0, paper_equity_sol=4.4)
    assert s["trades"] == 8 and s["pnl_pct"] > 0 and round(s["paper_pnl_pct"]) == 10
    checks = {c["id"]: c["ok"] for c in d.readiness(d.closed)}
    assert checks == {"sample": False, "profit": True, "breadth": True, "drawdown": True, "stuck": True}   # 8 < 50 trades


def test_every_fill_and_failed_attempt_is_on_the_live_tape(settings):
    jup = FakeJupiter()
    d = _dry(settings, jup)
    asyncio.run(d.buy(_buy()))
    jup.down.add((MINT, SOL_MINT))
    asyncio.run(d.sell(_sell()))                             # nobody buys it for now
    jup.down.clear()
    jup.price = 2.6e-6
    asyncio.run(d.retry_stuck_sells())
    sides = [t["side"] for t in d.tape]
    assert sides == ["buy", "sell_failed", "sell"]
    buy, sell = d.tape[0], d.tape[-1]
    assert buy["price"] > 0 and buy["paper_price"] == 1e-6 and buy["route"] == "Pump.fun"
    assert sell["pnl_sol"] > 0 and sell["paper_pnl_pct"] == 150.0 and not sell["partial"]
    assert [t["side"] for t in d.view(100.0, 4.0)["tape"]] == ["sell", "sell_failed", "buy"]   # newest first


def test_positions_keep_a_value_path_for_their_chart(settings):
    store = Store(settings.db_path)
    jup = FakeJupiter()
    now = [1000.0]
    d = _dry(settings, jup, store, now=now)
    asyncio.run(d.buy(_buy()))
    for px in (1.2e-6, 1.6e-6):
        now[0] += 20
        jup.price = px
        asyncio.run(d.mark())
    pos = d.positions[("holder_dip@v1", MINT)]
    assert len(pos.path) == 3 and pos.path[-1][1] > pos.path[0][1]          # entry, then two re-checks going up
    assert d.view(100.0, 4.0)["open"][0]["path"] == pos.path
    now[0] += 20
    jup.price = 2.6e-6
    asyncio.run(d.sell(_sell()))
    assert len(d.closed[-1]["path"]) == 4                                    # ...and the sale
    assert "path" not in store.dryrun_trades()[0]                            # the chart lives in the wallet file only
    assert len(d.series[-1]) == 3 and d.series[-1][2] == d.cash              # wallet history: time, value, cash


def test_the_loss_limit_stops_it_and_a_new_attempt_starts_fresh(settings):
    store = Store(settings.db_path)
    jup = FakeJupiter()
    settings.dryrun.watcher = False                           # this test is about the loss limit, not the watcher
    d = _dry(settings, jup, store)
    settings.dryrun.loss_limit_pct = 10
    d.positions.clear()
    asyncio.run(d.buy(_buy()))
    jup.price = 0.1e-6                                       # the coin collapses: the wallet falls past -10%
    asyncio.run(d.mark())
    assert d.stopped and "loss limit" in d.stopped["reason"]
    asyncio.run(d.buy(dict(_buy(), mint="Other" + MINT[5:])))
    assert d.counts["after_stop"] == 1 and len(d.positions) == 1  # no new buys after the stop
    assert d.summary(100.0, 4.0)["stopped"] and d.tape[-1]["side"] == "stopped"

    d.market_fn = lambda: (100.0, 4.4)                       # the dashboard button / CLI writes a request
    (settings.data_dir / "session" / "dryrun_request.json").write_text('{"action": "new_attempt", "why": "test"}')
    d._handle_request()
    assert d.attempt == 2 and not d.stopped and not d.positions and abs(d.equity() - 0.5) < 1e-9   # fresh $50
    a = d.attempts[0]
    assert a["attempt"] == 1 and a["pnl_pct"] < -10 and "loss limit" in a["ended_by"] and a["trades"] == 1
    assert store.dryrun_trades()[0]["attempt"] == 1                                            # history kept per attempt
    assert any(t.get("attempt") == 1 for t in d.tape) and d.view(100.0, 4.4)["tape"] == []     # tape kept, view is #2's
    again = DryRun(settings, None, quote_fn=jup.quote, decimals_fn=six)
    assert again.load() and again.attempt == 2 and len(again.attempts) == 1


def test_paper_same_period_survives_a_champions_wallet_restart(settings):
    from solmeme.sim.dryrun import DryRun

    d = DryRun(settings)
    d.start_fresh(100.0, 0.20)                     # live attempt starts while paper sits at 0.20 SOL
    d.paper_rebase(0.22, 0.45)                     # paper made +10%, then a new champion restarts its wallet at 0.45
    s = d.summary(100.0, 0.45 * 0.9)               # the new wallet then loses 10%
    assert round(s["paper_pnl_pct"], 1) == round((1.10 * 0.90 - 1) * 100, 1)        # -1%, not +102%


def test_open_positions_show_the_exit_plan_and_mcap(settings, specs):
    from solmeme.ui.launch import _enrich_open

    sp = specs["kol_follow"]
    class St:
        price_sol = 2e-7
    class Market:
        sol_usd = 100.0
        def get(self, m): return St()
    class R:
        key = sp.key; spec = sp
    class Eng:
        market = Market(); runners = [R()]
    class S:
        engine = Eng()
    S.settings = settings
    rows = [dict(mint="M" * 40, key=sp.key)]
    _enrich_open(S, rows, 100.0)
    assert rows[0]["mcap_usd"] == 2e-7 * 1e9 * 100.0
    assert rows[0]["exit_plan"] and any("sell" in x or "stop" in x for x in rows[0]["exit_plan"])


def test_without_mev_protection_a_buy_can_be_sandwiched_up_to_the_slippage_limit(settings):
    settings.fills.jito_tip_sol = 0.0
    settings.fills.sandwich_prob_unprotected = 1.0           # always sandwiched, for the test
    settings.fills.sandwich_extra_unprotected_pct = 6.0
    jup = FakeJupiter(impact=2.0)
    d = _dry(settings, jup)
    asyncio.run(d.buy(_buy()))
    pos = d.positions[("holder_dip@v1", MINT)]
    clean = jup.calls[0][2] / LAMPORTS / 1e-6 * 0.98 * 1e6    # raw tokens the quote promised
    assert abs(pos.raw - int(clean / 1.06)) <= 1 and d.tape[-1]["sandwiched"] is True
    settings.fills.sandwich_extra_unprotected_pct = 80.0      # a sandwich can't push past the slippage limit
    d2 = _dry(settings, FakeJupiter(impact=2.0))
    asyncio.run(d2.buy(_buy()))
    limit = 1e-6 * (1 + d2.cfg.slippage_tolerance_pct / 100)
    assert d2.positions[("holder_dip@v1", MINT)].entry_price == __import__("pytest").approx(limit, rel=1e-6)


def test_fees_follow_the_trade_settings(settings):
    settings.fills.priority_fee_sol, settings.fills.jito_tip_sol = 0.0002, 0.0
    d = _dry(settings, FakeJupiter())
    assert abs(d._leg_cost(True) - (0.000005 + 0.0002)) < 1e-9 and d._leg_cost(False) == d._leg_cost(True)


def test_a_coin_paired_with_another_token_is_never_bought_live(settings):
    async def coin(mint):
        return {"quoteMint": "CARDSccUMFKoPRZxt5vt3ksUbxEFEcnZ3H2pd3dKxYjp"}
    async def sol_coin(mint):
        return {"quoteMint": "11111111111111111111111111111111"}
    jup = FakeJupiter()
    settings.dryrun.enabled = True
    settings.dryrun.start_usd = 50
    d = DryRun(settings, None, quote_fn=jup.quote, decimals_fn=six, clock=lambda: 1000.0, coin_fn=coin)
    d.start_fresh(sol_usd=100.0, paper_equity_sol=4.0)
    asyncio.run(d.buy(_buy()))
    assert not d.positions and d.tape[-1]["side"] == "blocked" and "not SOL" in d.tape[-1]["why"]
    assert not jup.calls                                         # never even asked Jupiter
    d2 = DryRun(settings, None, quote_fn=jup.quote, decimals_fn=six, clock=lambda: 1000.0, coin_fn=sol_coin)
    d2.start_fresh(sol_usd=100.0, paper_equity_sol=4.0)
    asyncio.run(d2.buy(_buy()))
    assert d2.positions                                          # paired with SOL: bought as usual


def test_the_watcher_sells_on_the_real_price_when_paper_never_does(settings):
    from types import SimpleNamespace as NS

    now = [1000.0]
    jup = FakeJupiter(impact=0.0)
    d = _dry(settings, jup, now=now)
    d.exit_plan_fn = lambda key: (35.0, 3600.0)              # the strategy's stop and time limit
    asyncio.run(d.buy(_buy()))
    pos = d.positions[("holder_dip@v1", MINT)]
    jup.price = 0.8e-6                                        # -20%: inside the stop, keep
    asyncio.run(d.mark())
    assert d.positions
    jup.price = 0.6e-6                                        # -40% on the real quote, paper still silent
    asyncio.run(d.mark())
    assert not d.positions and d.tape[-1]["side"] == "sell" and d.tape[-1]["why"].startswith("watcher: real value")

    d2 = _dry(settings, FakeJupiter(impact=0.0), now=now)
    d2.exit_plan_fn = lambda key: (35.0, 600.0)
    asyncio.run(d2.buy(_buy()))
    now[0] += 600 + 121                                       # past the strategy's max hold + grace
    asyncio.run(d2.mark())
    assert not d2.positions and "limit" in d2.tape[-1]["why"]

    now[0] = 1000.0
    d3 = _dry(settings, FakeJupiter(impact=0.0), now=now)
    d3.state_fn = lambda mint: NS(dev_peak_balance=1e8, dev_balance=3e7)   # the creator sold 70%
    asyncio.run(d3.buy(_buy()))
    asyncio.run(d3.mark())
    assert not d3.positions and "creator sold 70%" in d3.tape[-1]["why"]


def test_the_watcher_wont_buy_a_coin_already_falling_on_pump_fun(settings):
    async def falling(mint):
        return {"quoteMint": "11111111111111111111111111111111", "marketCapUsd": 3100.0, "athMarketCapUsd": 5800.0}
    jup = FakeJupiter()
    settings.dryrun.enabled, settings.dryrun.start_usd = True, 50
    d = DryRun(settings, None, quote_fn=jup.quote, decimals_fn=six, clock=lambda: 1000.0, coin_fn=falling)
    d.start_fresh(sol_usd=100.0, paper_equity_sol=4.0)
    asyncio.run(d.buy(_buy()))
    assert not d.positions and "under its high on pump.fun" in d.tape[-1]["why"] and not jup.calls


def test_a_failed_price_check_never_triggers_the_watchers_stop(settings):
    jup = FakeJupiter(impact=0.0)
    d = _dry(settings, jup)
    d.exit_plan_fn = lambda key: (25.0, 3600.0)
    asyncio.run(d.buy(_buy()))
    jup.down.add((MINT, SOL_MINT))                           # the sell quote fails once (Jupiter 503)
    asyncio.run(d.mark())
    assert d.positions                                       # "worth 0" from an error is not a -100% stop
    jup.down.clear()
    asyncio.run(d.mark())
    assert d.positions                                       # a good quote at the entry price: still holding


def test_the_watcher_wont_buy_from_a_coin_factory(settings):
    async def coin(mint):
        return {"quoteMint": "11111111111111111111111111111111", "dev": "Dev1", "marketCapUsd": 5000.0, "athMarketCapUsd": 5200.0}
    async def history(dev):
        return {"launches": 21, "graduated": 2}
    jup = FakeJupiter()
    settings.dryrun.enabled, settings.dryrun.start_usd = True, 50
    d = DryRun(settings, None, quote_fn=jup.quote, decimals_fn=six, clock=lambda: 1000.0, coin_fn=coin)
    d._dev_history = history
    d.start_fresh(sol_usd=100.0, paper_equity_sol=4.0)
    asyncio.run(d.buy(_buy()))
    assert not d.positions and "launched 21 coins" in d.tape[-1]["why"] and not jup.calls


def test_the_watcher_wont_buy_an_obvious_bundle(settings):
    from types import SimpleNamespace as NS
    async def coin(mint):
        return {"quoteMint": "11111111111111111111111111111111"}
    jup = FakeJupiter()
    settings.dryrun.enabled, settings.dryrun.start_usd = True, 50
    d = DryRun(settings, None, quote_fn=jup.quote, decimals_fn=six, clock=lambda: 1000.0, coin_fn=coin)
    d.state_fn = lambda mint: NS(ext={"bundletrace": {"checked": 9, "linked_wallets": 6, "linked_pct": 14.0}})
    d.start_fresh(sol_usd=100.0, paper_equity_sol=4.0)
    asyncio.run(d.buy(_buy()))
    assert not d.positions and "funded by the same wallet" in d.tape[-1]["why"]


def test_a_real_price_far_below_the_decision_price_means_broken_data_and_no_buy(settings):
    jup = FakeJupiter(price=1.38e-7, impact=0.0)
    d = _dry(settings, jup)
    asyncio.run(d.buy(_buy(price=2.5e-7, ref=2.5e-7)))        # LARP: paper 2.5e-7, real 1.38e-7
    assert not d.positions and d.tape[-1]["side"] == "missed" and d.tape[-1]["why"].startswith("data:")
    asyncio.run(d.buy(_buy(price=1.45e-7, ref=1.45e-7)))      # within 30%: a normal buy
    assert d.positions
