"""pump.fun bonding curve math (constant product over *virtual* reserves).

Reserves are in UI units: v_sol in SOL, v_tokens in whole tokens — the same
units PumpPortal streams (vSolInBondingCurve / vTokensInBondingCurve).
Price (SOL per token) = v_sol / v_tokens. Total supply = 1e9.
"""

from __future__ import annotations

TOTAL_SUPPLY = 1_000_000_000.0
INITIAL_V_TOKENS = 1_073_000_000.0
INITIAL_REAL_TOKENS = 793_100_000.0
INITIAL_V_SOL = 30.0


def price(v_sol: float, v_tokens: float) -> float:
    return v_sol / v_tokens if v_tokens > 0 else 0.0


def buy_out(v_sol: float, v_tokens: float, sol_in: float, fee_pct: float) -> tuple[float, float, float]:
    """Spend `sol_in` gross. Returns (tokens_out, new_v_sol, new_v_tokens)."""
    net = sol_in * (1 - fee_pct / 100)
    k = v_sol * v_tokens
    new_v_sol = v_sol + net
    new_v_tokens = k / new_v_sol
    return v_tokens - new_v_tokens, new_v_sol, new_v_tokens


def sell_out(v_sol: float, v_tokens: float, tokens_in: float, fee_pct: float) -> tuple[float, float, float]:
    """Sell `tokens_in`. Returns (sol_out_net, new_v_sol, new_v_tokens)."""
    k = v_sol * v_tokens
    new_v_tokens = v_tokens + tokens_in
    new_v_sol = k / new_v_tokens
    gross = v_sol - new_v_sol
    return gross * (1 - fee_pct / 100), new_v_sol, new_v_tokens


def bonding_pct(v_tokens: float) -> float:
    """Share of sellable curve tokens already bought (100 → graduation)."""
    pct = (INITIAL_V_TOKENS - v_tokens) / INITIAL_REAL_TOKENS * 100
    return max(0.0, min(100.0, pct))
