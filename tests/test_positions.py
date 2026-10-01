from __future__ import annotations

from solmeme.core.events import TokenLaunched
from solmeme.sim.curve import INITIAL_V_SOL, INITIAL_V_TOKENS
from solmeme.sim.engine import Engine
from solmeme.sim.portfolio import Position
from solmeme.sim.positions import position_rows

MINT = "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"


def _engine(settings, specs) -> Engine:
    return Engine([(specs["dex_curve_momentum"], "champion")], settings)


def _launch(ts: float) -> TokenLaunched:
    return TokenLaunched(
        ts=ts, source="test", mint=MINT, symbol="AI", name="AI", pool="pump",
        v_sol=INITIAL_V_SOL, v_tokens=INITIAL_V_TOKENS,
    )


def _pos(mint: str, symbol: str, entry_ts: float, **kw) -> Position:
    return Position(
        trade_id=mint, mint=mint, symbol=symbol, entry_ts=entry_ts,
        entry_price=kw.pop("entry_price", 1e-8),
        tokens=kw.pop("tokens", 1.0),
        initial_tokens=kw.pop("initial_tokens", 1.0),
        cost_sol=kw.pop("cost_sol", 0.1),
        **kw,
    )


def _trade(symbol: str, exit_ts: float, exit_reason: str) -> dict:
    return {
        "symbol": symbol,
        "strategy_id": "dex_curve_momentum",
        "mint": MINT,
        "size_sol": 0.1,
        "entry_price": 1e-8,
        "exit_price": 2e-8,
        "proceeds_sol": 0.2,
        "pnl_sol": 0.1,
        "exit_reason": exit_reason,
        "exit_ts": exit_ts,
    }


def test_no_positions_or_trades(settings, specs):
    assert position_rows(_engine(settings, specs)) == []


def test_open_position_row(settings, specs):
    eng = _engine(settings, specs)
    eng.step(_launch(1.0))
    st = eng.market.get(MINT)
    assert st is not None
    st.set_price(2.0, 2e-8)
    eng.market.sol_usd = 150
    held = 500_000.0
    eng.runners[0].portfolio.positions[MINT] = _pos(
        MINT, "AI", 10.0,
        entry_price=1e-8,
        tokens=held,
        initial_tokens=held * 2,
        cost_sol=0.100,
        proceeds_sol=0.090,
        fills=[{"side": "sell", "price": 2e-8, "sol": 0.09, "reason": "tp1"}],
    )
    rows = position_rows(eng)
    assert len(rows) == 1
    coin, strategy, status, bought, _entry, sold, mark, made = rows[0]
    assert coin == "AI"
    assert strategy == "dex_curve_momentum"
    assert status.startswith("holding")
    assert bought == "0.100 SOL"
    assert sold == "0.090 SOL"
    assert "2.00x" in mark and "tp1" in mark
    assert "SOL" in made and "$" in made


def test_closed_trades_follow_opens_and_respect_limit(settings, specs):
    eng = _engine(settings, specs)
    book = eng.runners[0].portfolio.positions
    book["early"] = _pos("earlymint", "EARLY", 1.0)
    book["late"] = _pos("latemint", "LATE", 9.0)
    eng.journal.trades.extend([
        _trade("DROP", 10, "stop"),
        _trade("NEW", 40, "tp1"),
        _trade("MID", 20, "trail"),
        _trade("NEXT", 30, "time_stop"),
    ])
    rows = position_rows(eng, closed_limit=2)
    assert [row[0] for row in rows] == ["LATE", "EARLY", "NEW", "NEXT"]
    assert rows[2][2].startswith("closed")
    assert rows[3][2].startswith("closed")
    assert "DROP" not in [row[0] for row in rows]
    assert "MID" not in [row[0] for row in rows]
