import asyncio
import time
from io import StringIO

import pytest
from rich.console import Console

from solmeme.config import PrefilterCfg
from solmeme.core.events import Enrichment, TokenLaunched
from solmeme.sim.activity import TokenActivity
from solmeme.sim.curve import INITIAL_V_SOL, INITIAL_V_TOKENS
from solmeme.sim.engine import Engine
from solmeme.sim.live import LiveSession, _wallet_line
from solmeme.sim.portfolio import Position
from solmeme.sources.watchlist import Watchlist

MINT = "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"


def _launch(ts: float) -> TokenLaunched:
    return TokenLaunched(
        ts=ts, source="test", mint=MINT, symbol="OTTR", name="Otter", pool="pump",
        v_sol=INITIAL_V_SOL, v_tokens=INITIAL_V_TOKENS,
    )


def test_step_records_analyzing_note(settings, specs):
    activity = TokenActivity()
    eng = Engine([(specs["dex_curve_momentum"], "champion")], settings, activity=activity)
    now = time.time()
    watch = Watchlist()
    eng.step(_launch(now - 120))
    assert activity.board(watch, {}, {}, now) == []
    eng.step(Enrichment(ts=now, source="dex", mint=MINT, data={"dex_id": "pumpfun"}))
    notes = [n for n in activity.board(watch, {MINT: "OTTR"}, {}, now) if n.mint == MINT]
    assert len(notes) == 1
    assert notes[0].phase == "analyzing"
    assert notes[0].detail == "no entry"
    assert notes[0].symbol == "OTTR"


def test_step_records_prefilter_skip(settings, specs):
    settings.prefilter = PrefilterCfg(enabled=True, all=[{"id": "cheap", "f": "mcap_sol", "op": "<", "v": 0}])
    activity = TokenActivity()
    eng = Engine([(specs["dex_curve_momentum"], "champion")], settings, activity=activity)
    now = time.time()
    eng.step(_launch(now - 120))
    eng.step(Enrichment(ts=now, source="dex", mint=MINT, data={"dex_id": "pumpfun"}))
    notes = activity.board(Watchlist(), {}, {}, now)
    assert len(notes) == 1
    assert notes[0].phase == "skipped"
    assert notes[0].detail == "prefilter cheap"
    assert eng.prefilter_blocked["cheap"] == 1


def test_live_session_prints_token_board(settings, specs):
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=120)
    session = LiveSession(
        settings, None, [(specs["dex_curve_momentum"], "champion")],
        console=console, warmup_min=0, ai=False,
    )
    assert isinstance(session.activity, TokenActivity)
    assert session.engine is not None
    assert session.engine.activity is session.activity
    session.engine.market.sol_usd = 150.0
    session.print_status()
    text = buf.getvalue()
    assert "tokens" in text
    assert "SOL $150.00" in text
    assert "$1,500" in text
    assert "positions" in text
    assert "no paper buys yet" in text


def test_live_session_prints_open_position(settings, specs):
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=200)
    session = LiveSession(
        settings, None, [(specs["dex_curve_momentum"], "champion")],
        console=console, warmup_min=0, ai=False,
    )
    session.engine.runners[0].portfolio.positions[MINT] = Position(
        trade_id="t1",
        mint=MINT,
        symbol="AI",
        entry_ts=time.time(),
        entry_price=1e-8,
        tokens=50,
        initial_tokens=100,
        cost_sol=0.1,
        proceeds_sol=0.09,
        fills=[{"side": "sell", "price": 2e-8, "reason": "tp1"}],
    )
    session.engine.market.sol_usd = 150
    session.print_status()
    text = buf.getvalue()
    assert "AI" in text
    assert "dex_curve_momentum" in text
    assert "tp1" in text
    assert "0.100 SOL" in text


def test_wallet_line_formats_gain_loss_goal_and_sol_only():
    view = {
        "best": "near_graduation@x", "start_sol": 0.4, "start_usd": 50.0, "balance_sol": 0.5,
        "balance_usd": 62.5, "pnl_sol": 0.1, "pnl_usd": 12.5, "pnl_pct": 25.0,
        "goal_usd": 100.0, "goal_pct": 62.5, "reached": [],
    }
    up = _wallet_line(view)
    assert "$50.00 (0.400 SOL) → $62.50" in up
    assert "[green]+$12.50 (+25.00%)[/green]" in up
    assert "best: near_graduation@x" in up
    assert "Goal[/bold] $100.00 (62%)" in up
    down = _wallet_line({**view, "balance_usd": 40.0, "pnl_usd": -10.0, "pnl_pct": -20.0})
    assert "[red]-$10.00 (-20.00%)[/red]" in down
    sol_only = _wallet_line({**view, "start_usd": None, "balance_usd": None, "pnl_usd": None,
                             "goal_pct": None})
    assert "0.400 SOL → 0.500 SOL" in sol_only
    assert "+0.100 SOL (+25.00%)" in sol_only
    assert _wallet_line(None) == "[bold]Wallet[/bold]  —"


def test_live_session_funds_each_strategy_in_dollars(settings, specs, monkeypatch):
    monkeypatch.setattr("solmeme.sim.live.fetch_sol_usd", lambda: 125.0)
    settings.portfolio.starting_usd = 50.0
    settings.portfolio.goal_usd = 100.0
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=240)
    session = LiveSession(
        settings, None,
        [(specs["dex_curve_momentum"], "champion"), (specs["near_graduation"], "champion")],
        console=console, warmup_min=0, ai=False,
    )
    for runner in session.engine.runners:
        assert runner.portfolio.starting_sol == pytest.approx(0.4)
    loser, winner = session.engine.runners
    loser.portfolio.cash -= 0.1
    winner.portfolio.cash += 0.1

    # SOL went up since the wallet was funded: the dollar result includes that move
    session.engine.market.sol_usd = 150.0
    view = session.wallet()
    assert view["best"] == winner.key
    assert view["start_usd"] == pytest.approx(50.0)
    assert view["balance_usd"] == pytest.approx(75.0)
    assert view["pnl_usd"] == pytest.approx(25.0)
    assert view["pnl_pct"] == pytest.approx(50.0)
    assert view["goal_pct"] == pytest.approx(75.0)
    assert view["reached"] == []

    winner.portfolio.cash += 0.2
    assert session.wallet()["reached"] == [winner.key]

    session.print_status()
    text = buf.getvalue()
    assert "$50.00 = 0.4000 SOL per strategy at $125.00/SOL" in text
    assert "Wallet" in text and "Goal $100.00" in text


def test_wallet_tracks_high_and_low_since_start(settings, specs):
    buf = StringIO()
    session = LiveSession(
        settings, None, [(specs["dex_curve_momentum"], "champion")],
        console=Console(file=buf, force_terminal=False, width=260), warmup_min=0, ai=False,
    )
    runner = session.engine.runners[0]
    start = runner.portfolio.starting_sol
    session.engine.market.sol_usd = 100.0
    for t, delta in ((1.0, 0.0), (2.0, 2.0), (3.0, -1.0), (4.0, 0.5)):
        runner.portfolio.cash = start + delta
        session.wallet_history.observe(session.engine, 100.0, t)
    rng = session.wallet()["range"]
    assert rng["unit"] == "usd"
    assert rng["high"] == pytest.approx((start + 2) * 100)
    assert rng["high_ts"] == 2.0
    assert rng["low"] == pytest.approx((start - 1) * 100)
    assert rng["low_ts"] == 3.0
    assert rng["from_high_pct"] == pytest.approx(-1.5 / (start + 2) * 100)

    session.print_status()
    text = buf.getvalue()
    assert f"High ${(start + 2) * 100:,.2f}" in text
    assert f"Low ${(start - 1) * 100:,.2f}" in text


def test_wallet_range_falls_back_to_sol_before_a_price(settings, specs):
    session = LiveSession(
        settings, None, [(specs["dex_curve_momentum"], "champion")],
        console=Console(file=StringIO()), warmup_min=0, ai=False,
    )
    runner = session.engine.runners[0]
    session.wallet_history.observe(session.engine, None, 1.0)
    runner.portfolio.cash += 1.0
    rng = session.wallet()["range"]
    assert rng["unit"] == "sol"
    assert rng["high"] == pytest.approx(runner.portfolio.cash)   # current value beats the last mark
    assert rng["high_ts"] is None
    assert rng["from_high_pct"] == 0


def test_live_session_keeps_sol_start_without_a_price(settings, specs, monkeypatch):
    monkeypatch.setattr("solmeme.sim.live.fetch_sol_usd", lambda: None)
    monkeypatch.setattr("solmeme.sim.live.logged_sol_usd", lambda s: None)
    settings.portfolio.starting_usd = 50.0
    session = LiveSession(
        settings, None, [(specs["dex_curve_momentum"], "champion")],
        console=Console(file=StringIO()), warmup_min=0, ai=False,
    )
    assert session.engine.runners[0].portfolio.starting_sol == settings.portfolio.starting_sol


def test_run_stays_up_without_a_time_limit(settings, specs, monkeypatch):
    """create_task() rejects gather()'s future on Python 3.11, which used to exit ./run immediately."""

    class _Quiet:
        async def run(self):
            await asyncio.sleep(30)

    monkeypatch.setattr("solmeme.sim.live.build_sources", lambda ctx: ([_Quiet()], None))
    session = LiveSession(
        settings, None, [(specs["dex_curve_momentum"], "champion")],
        warmup_min=0, ai=False,
    )

    async def go():
        task = asyncio.create_task(session.run(None))
        await asyncio.sleep(0.3)
        if task.done():
            return task.exception()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return None

    assert asyncio.run(go()) is None
