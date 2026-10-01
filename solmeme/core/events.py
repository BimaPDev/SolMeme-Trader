"""Normalized event model. Every source emits these; the event log stores them;
live and replay feed the exact same objects into the engine.

`ts` is the local receive time (epoch seconds) — the moment our bot could
have known about the event. Features must only ever use events with ts <= now.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _Ev(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=False)
    ts: float
    source: str = ""


class TokenLaunched(_Ev):
    kind: Literal["launch"] = "launch"
    mint: str
    name: str = ""
    symbol: str = ""
    creator: str = ""
    pool: str = "pump"
    v_sol: float | None = None
    v_tokens: float | None = None
    mcap_sol: float | None = None
    initial_buy_sol: float = 0.0
    initial_buy_tokens: float = 0.0
    uri: str = ""


class CurveTrade(_Ev):
    """Bonding-curve trade with post-trade virtual reserves (exact fill sim)."""
    kind: Literal["curve_trade"] = "curve_trade"
    mint: str
    trader: str
    side: Literal["buy", "sell"]
    sol: float
    tokens: float
    new_balance: float | None = None
    v_sol: float
    v_tokens: float
    mcap_sol: float | None = None
    pool: str = "pump"
    sig: str = ""


class PoolTrade(_Ev):
    """Post-migration AMM trade (PumpSwap / Raydium / Meteora)."""
    kind: Literal["pool_trade"] = "pool_trade"
    mint: str
    trader: str = ""
    side: Literal["buy", "sell"]
    sol: float
    tokens: float
    pool: str = "pump-amm"
    mcap_sol: float | None = None
    sig: str = ""


class Migration(_Ev):
    kind: Literal["migration"] = "migration"
    mint: str
    pool: str = "pump-amm"


class WalletTrade(_Ev):
    """A trade by a tracked / smart-money / KOL wallet."""
    kind: Literal["wallet_trade"] = "wallet_trade"
    wallet: str
    label: str = "tracked"          # tracked | smartmoney | kol
    mint: str
    side: Literal["buy", "sell"]
    sol: float | None = None
    usd: float | None = None
    symbol: str = ""
    tx: str = ""


class Enrichment(_Ev):
    """Point-in-time facts about a token from an API (security, holders, socials, price)."""
    kind: Literal["enrichment"] = "enrichment"
    mint: str
    data: dict[str, Any] = Field(default_factory=dict)


class SolPrice(_Ev):
    kind: Literal["sol_price"] = "sol_price"
    usd: float


class ExternalSignal(_Ev):
    """Discrete signal from a third-party tool (GMGN trench, Dex boost, Axiom, j7, fomo…)."""
    kind: Literal["signal"] = "signal"
    mint: str
    signal: str
    data: dict[str, Any] = Field(default_factory=dict)


Event = Annotated[
    Union[TokenLaunched, CurveTrade, PoolTrade, Migration, WalletTrade, Enrichment, SolPrice, ExternalSignal],
    Field(discriminator="kind"),
]
EVENT_ADAPTER: TypeAdapter = TypeAdapter(Event)


def parse_event(line: str | bytes):
    return EVENT_ADAPTER.validate_json(line)


def dump_event(ev) -> str:
    return ev.model_dump_json(exclude_defaults=False)
