import time
from io import StringIO

from rich.console import Console

from solmeme.config import PrefilterCfg
from solmeme.core.events import Enrichment, TokenLaunched
from solmeme.sim.activity import TokenActivity
from solmeme.sim.curve import INITIAL_V_SOL, INITIAL_V_TOKENS
from solmeme.sim.engine import Engine
from solmeme.sim.live import LiveSession
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
