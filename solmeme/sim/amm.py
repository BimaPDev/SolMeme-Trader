"""Constant-product AMM approximation for migrated tokens (PumpSwap/Raydium/Meteora).

Pool reserves are seeded at migration so the pool price equals the last curve
price, then updated from observed pool trades and re-anchored from DexScreener
price/liquidity when available.
"""

from __future__ import annotations

from dataclasses import dataclass

MIGRATED_TOKEN_RESERVE = 206_900_000.0  # tokens moved into the pool at graduation


@dataclass
class Pool:
    sol: float
    tokens: float

    @property
    def price(self) -> float:
        return self.sol / self.tokens if self.tokens > 0 else 0.0

    @classmethod
    def seed_from_price(cls, price_sol: float, token_reserve: float = MIGRATED_TOKEN_RESERVE) -> "Pool":
        return cls(sol=price_sol * token_reserve, tokens=token_reserve)

    def buy_out(self, sol_in: float, fee_pct: float) -> float:
        net = sol_in * (1 - fee_pct / 100)
        k = self.sol * self.tokens
        return self.tokens - k / (self.sol + net)

    def sell_out(self, tokens_in: float, fee_pct: float) -> float:
        k = self.sol * self.tokens
        gross = self.sol - k / (self.tokens + tokens_in)
        return gross * (1 - fee_pct / 100)

    def apply_trade(self, side: str, sol: float, tokens: float) -> None:
        if side == "buy":
            self.sol += sol
            self.tokens = max(1.0, self.tokens - tokens)
        else:
            self.sol = max(1e-9, self.sol - sol)
            self.tokens += tokens

    def reanchor(self, price_sol: float, liquidity_sol: float | None) -> None:
        """Match an external price (and optionally total liquidity in SOL)."""
        if price_sol <= 0:
            return
        sol_side = liquidity_sol / 2 if liquidity_sol and liquidity_sol > 0 else self.sol
        self.sol = sol_side
        self.tokens = sol_side / price_sol
