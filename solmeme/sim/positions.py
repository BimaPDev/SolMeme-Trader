"""Paper-position rows for the live board."""

from __future__ import annotations

from solmeme.sim.activity import short_mint
from solmeme.sim.engine import Engine

_MISSING = "—"


def position_rows(
    engine: Engine, *, closed_limit: int = 8,
) -> list[tuple[str, str, str, str, str, str, str, str]]:
    """Open positions, newest entry first, then up to closed_limit closed trades.

    Columns: coin, strategy, status, bought, entry, sold, mark, made.
    """
    rows = [_open_row(engine, runner, pos) for runner, pos in _open_positions(engine)]
    rows.extend(_closed_row(engine, trade) for trade in _closed_trades(engine, closed_limit))
    return rows


def position_values(engine: Engine, *, closed_limit: int = 8) -> list[tuple[str, float, float]]:
    """(mint, invested SOL, worth now SOL) for the same rows, in the same order, as position_rows.
    Worth now = SOL already sold for plus what is still held at market; minus invested = made."""
    out = [(pos.mint, pos.cost_sol, pos.proceeds_sol + _held_value(engine, pos)) for _, pos in _open_positions(engine)]
    for t in _closed_trades(engine, closed_limit):
        size = float(t.get("size_sol") or 0.0)
        out.append((str(t.get("mint") or ""), size, size + float(t.get("pnl_sol") or 0.0)))
    return out


def position_owners(engine: Engine, *, closed_limit: int = 8) -> list[tuple[str, str]]:
    """(strategy key, role) for the same rows, in the same order, as position_rows."""
    roles = {r.key: r.label for r in engine.runners}
    out = [(runner.key, runner.label) for runner, _ in _open_positions(engine)]
    for t in _closed_trades(engine, closed_limit):
        key = f"{t.get('strategy_id')}@{t.get('version')}"
        out.append((key, roles.get(key, "")))
    return out


def _open_positions(engine: Engine) -> list[tuple]:
    found = [
        (pos.entry_ts, runner, pos)
        for runner in engine.runners
        for pos in runner.portfolio.positions.values()
    ]
    found.sort(key=lambda item: item[0], reverse=True)
    return [(runner, pos) for _, runner, pos in found]


def _closed_trades(engine: Engine, limit: int) -> list[dict]:
    if limit <= 0:
        return []
    trades = sorted(engine.journal.trades, key=lambda t: t.get("exit_ts") or 0.0, reverse=True)
    return trades[:limit]


def _open_row(engine: Engine, runner, pos) -> tuple[str, str, str, str, str, str, str, str]:
    sells = [fill for fill in pos.fills if fill.get("side") == "sell"]
    if pos.selling:
        status = "selling"
    elif sells:
        left = pos.tokens / pos.initial_tokens * 100 if pos.initial_tokens else 0.0
        status = f"holding {left:.0f}%"
    else:
        status = "holding"
    if sells:
        last = sells[-1]
        mark = _multiple(last.get("price"), pos.entry_price, str(last.get("reason") or ""))
        sold = _sol(pos.proceeds_sol)
    else:
        st = engine.market.get(pos.mint)
        mark = _multiple(st.price_sol if st else None, pos.entry_price, "now")
        sold = _MISSING
    made = pos.proceeds_sol + _held_value(engine, pos) - pos.cost_sol
    return (
        _coin(pos.symbol, pos.mint),
        runner.spec.id,
        status,
        _sol(pos.cost_sol),
        _px(pos.entry_price),
        sold,
        mark,
        _pnl(made, engine.market.sol_usd),
    )


def _closed_row(engine: Engine, trade: dict) -> tuple[str, str, str, str, str, str, str, str]:
    reason = str(trade.get("exit_reason") or "").strip()
    return (
        _coin(trade.get("symbol"), trade.get("mint")),
        str(trade.get("strategy_id") or ""),
        f"closed {reason}".strip(),
        _sol(trade.get("size_sol")),
        _px(trade.get("entry_price")),
        _sol(trade.get("proceeds_sol")),
        _multiple(trade.get("exit_price"), trade.get("entry_price"), reason),
        _pnl(float(trade.get("pnl_sol") or 0.0), engine.market.sol_usd),
    )


def _held_value(engine: Engine, pos) -> float:
    if pos.tokens <= 0:
        return 0.0
    st = engine.market.get(pos.mint)
    if st is None:
        return 0.0
    got = engine.fills.liquidation_value(st, pos.tokens)
    if got is None:
        if st.price_sol:
            return pos.tokens * st.price_sol
        return 0.0
    return got


def _coin(symbol, mint) -> str:
    if symbol:
        return str(symbol)
    return short_mint(mint or "")


def _px(price) -> str:
    try:
        price = float(price)
    except (TypeError, ValueError):
        return _MISSING
    if price <= 0:
        return _MISSING
    return f"{price:.4g}"


def _sol(amount) -> str:
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        amount = 0.0
    return f"{amount:.3f} SOL"


def _multiple(num, den, label: str) -> str:
    try:
        num = float(num)
        den = float(den)
    except (TypeError, ValueError):
        return _MISSING
    if num <= 0 or den <= 0:
        return _MISSING
    return f"{num / den:.2f}x {label}"


def _usd(amount: float) -> str:
    body = f"${abs(amount):,.2f}" if abs(amount) < 100 else f"${abs(amount):,.0f}"
    if amount < 0:
        return "-" + body
    if amount > 0:
        return "+" + body
    return body


def _pnl(sol: float, sol_usd: float | None) -> str:
    text = f"{sol:+.3f} SOL"
    if sol_usd:
        text += f" ({_usd(sol * sol_usd)})"
    return text
