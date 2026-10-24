"""SOL-denominated portfolio for one strategy version (rewrite of PaperTrader
engine/portfolio.py: partial exits, all costs tracked, rent refunds)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass
class Position:
    trade_id: str
    mint: str
    symbol: str
    entry_ts: float
    entry_price: float
    tokens: float
    initial_tokens: float
    cost_sol: float                  # SOL spent incl. every cost (swap + network + platform + rent)
    rent_sol: float = 0.0
    proceeds_sol: float = 0.0        # SOL received from partial/total exits, net of costs
    fees_sol: float = 0.0
    failed_attempts: int = 0
    peak_price: float = 0.0
    trough_price: float = 0.0
    tp_done: int = 0
    entry_features: dict = field(default_factory=dict)
    fills: list = field(default_factory=list)
    kol_sells_at_entry: int = 0
    dev_sold_at_entry: bool = False
    selling: bool = False            # a sell order is in flight


class Account:
    """SOL cash. Every strategy's Portfolio can draw on one Account to trade a shared wallet."""

    def __init__(self, starting_sol: float):
        self.starting_sol = starting_sol
        self.cash = starting_sol


class Portfolio:
    def __init__(self, starting_sol: float, account: Account | None = None):
        self.starting_sol = starting_sol
        self.account = account or Account(starting_sol)
        self.positions: dict[str, Position] = {}
        self.realized = 0.0
        self.day_pnl: dict[str, float] = {}
        self.closed = 0
        self.wins = 0
        self.fees = 0.0

    @property
    def cash(self) -> float:
        return self.account.cash

    @cash.setter
    def cash(self, value: float) -> None:
        self.account.cash = value

    def charge(self, sol: float) -> None:
        """Costs of failed attempts (priority fee etc.)."""
        self.cash -= sol
        self.fees += sol

    def open(self, pos: Position, fill_sol: float, fill_cost: float) -> None:
        self.cash -= fill_sol + fill_cost
        self.fees += fill_cost
        self.positions[pos.mint] = pos

    def reduce(self, mint: str, tokens: float, sol_out: float, cost: float) -> Position:
        pos = self.positions[mint]
        tokens = min(tokens, pos.tokens)
        pos.tokens -= tokens
        net = sol_out - cost
        pos.proceeds_sol += net
        pos.fees_sol += cost
        self.cash += net
        self.fees += cost
        return pos

    def close(self, mint: str, ts: float) -> tuple[Position, float]:
        pos = self.positions.pop(mint)
        # rent refunded when the token account is closed
        self.cash += pos.rent_sol
        pos.proceeds_sol += pos.rent_sol
        pnl = pos.proceeds_sol - pos.cost_sol
        self.realized += pnl
        day = utc_day(ts)
        self.day_pnl[day] = self.day_pnl.get(day, 0.0) + pnl
        self.closed += 1
        if pnl > 0:
            self.wins += 1
        return pos, pnl

    def day_loss(self, ts: float) -> float:
        return -min(0.0, self.day_pnl.get(utc_day(ts), 0.0))
