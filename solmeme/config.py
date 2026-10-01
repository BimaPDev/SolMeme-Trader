"""Settings: config/settings.yaml for everything tunable, .env for secrets."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


class RecordCfg(BaseModel):
    trade_stream_daily_events: int = 30000
    watch_launch_window_s: float = 600
    watch_ttl_s: float = 1800
    post_exit_track_s: float = 1800
    max_subscriptions: int = 300
    retention_days: int = 30


class PumpPortalCfg(BaseModel):
    enabled: bool = True
    url: str = "wss://pumpportal.fun/api/data"


class GmgnCfg(BaseModel):
    enabled: bool = True
    rate_per_s: float = 2.0
    trenches_poll_s: float = 10
    trench_limit: int = 80
    trench_types: list[str] = ["new_creation", "near_completion", "completed"]
    wallet_feeds: list[str] = ["smartmoney", "kol"]
    wallet_feed_poll_s: float = 15
    tracked_wallet_poll_s: float = 30
    security_max_per_min: int = 20


class DexCfg(BaseModel):
    enabled: bool = True
    boosts_poll_s: float = 60
    watch_poll_s: float = 5
    max_requests_per_min: int = 240


class JupCfg(BaseModel):
    enabled: bool = True
    sol_price_poll_s: float = 60   # CoinGecko by default; Jupiter Price v3 if JUPITER_API_KEY is set


class Toggle(BaseModel):
    model_config = ConfigDict(extra="allow")   # adapter-specific options (read via model_extra)
    enabled: bool = False


class SourcesCfg(BaseModel):
    pumpportal: PumpPortalCfg = PumpPortalCfg()
    gmgn: GmgnCfg = GmgnCfg()
    dexscreener: DexCfg = DexCfg()
    jupiter: JupCfg = JupCfg()
    rugcheck: Toggle = Toggle()
    tracked_wallets: list[str] = []
    axiom: Toggle = Toggle()
    j7tracker: Toggle = Toggle()
    fomo: Toggle = Toggle()


class FillsCfg(BaseModel):
    latency_ms: tuple[float, float] = (400, 1500)
    slippage_tolerance_pct: float = 25
    sell_slippage_tolerance_pct: float = 50
    sell_max_retries: int = 3
    fail_rate: float = 0.10
    priority_fee_sol: float = 0.001
    jito_tip_sol: float = 0.0005
    platform_fee_pct: float = 0.5
    ata_rent_sol: float = 0.00204
    curve_fee_pct: float = 1.25
    amm_fee_tiers: list[tuple[float, float]] = [(420, 1.25), (2000, 0.95), (20000, 0.60), (98240, 0.45), (1e12, 0.30)]
    sandwich_prob: float = 0.15
    sandwich_extra_pct: float = 3.0
    stale_exit_haircut_pct: float = 30

    def amm_fee_pct(self, mcap_sol: float | None) -> float:
        m = mcap_sol or 0.0
        for cap, fee in self.amm_fee_tiers:
            if m <= cap:
                return fee
        return self.amm_fee_tiers[-1][1]


class PortfolioCfg(BaseModel):
    starting_sol: float = 10.0
    max_position_sol: float = 1.0
    daily_loss_limit_sol: float = 3.0


class PrefilterCfg(BaseModel):
    """Global hard rules (same DSL as strategies). A token must pass these before ANY strategy
    evaluates it — and therefore before anything about it can ever reach the AI."""
    enabled: bool = True
    all: list[dict] = []
    none: list[dict] = []


class TriageCfg(BaseModel):
    """Deterministic checks that decide whether a review is worth an AI call at all."""
    min_trades: int = 15                 # fewer trades → sample noise, never call the AI
    healthy_expectancy_pct: float = 15   # champion doing this well and not decaying → skip
    min_feature_auc_edge: float = 0.12   # |AUC-0.5| of a winner/loser feature worth reporting
    near_miss_2x_share: float = 0.35     # a rule blocks tokens that ran 2x this often → worth a look
    early_exit_share: float = 0.30       # share of exits after which the token ran ≥2x
    rug_hit_rate: float = 0.10
    fee_drag_share: float = 0.35         # fees / gross wins


class AiCfg(BaseModel):
    triage: TriageCfg = TriageCfg()
    provider: str = "claude"
    model: str = "claude-opus-5-5"
    effort: str = "high"
    max_tokens: int = 16000
    daily_budget_usd: float = 5.0
    review_every_trades: int = 40
    review_every_hours: float = 12
    max_proposals_per_review: int = 3
    max_challengers_per_strategy: int = 3
    price_in: float = 4.0
    price_out: float = 20.0
    price_cache_read: float = 0.20
    price_cache_write: float = 5.0
    openai_compat: dict[str, Any] = Field(default_factory=dict)
    hermes_ssh: dict[str, Any] = Field(default_factory=dict)


class PromotionCfg(BaseModel):
    review_window_days: float = 3
    holdout_frac: float = 0.3
    min_backtest_trades: int = 20
    min_expectancy_delta_sol: float = 0.0
    max_dd_tolerance: float = 1.2
    min_shadow_trades: int = 30
    max_shadow_hours: float = 72
    bootstrap_conf: float = 0.90
    bootstrap_iters: int = 2000


class SniperCfg(BaseModel):
    """Paper-only launch sniper. It never sends a transaction."""
    enabled: bool = True
    decision_s: float = 12
    horizon_s: float = 600
    explore: bool = True


class Settings(BaseModel):
    data_dir: Path = Path("data")
    strategies_dir: Path = Path("strategies")
    record: RecordCfg = RecordCfg()
    sources: SourcesCfg = SourcesCfg()
    fills: FillsCfg = FillsCfg()
    portfolio: PortfolioCfg = PortfolioCfg()
    prefilter: PrefilterCfg = PrefilterCfg()
    ai: AiCfg = AiCfg()
    promotion: PromotionCfg = PromotionCfg()
    sniper: SniperCfg = SniperCfg()
    excluded_mints: list[str] = []

    def resolve(self) -> "Settings":
        if not self.data_dir.is_absolute():
            self.data_dir = ROOT / self.data_dir
        if not self.strategies_dir.is_absolute():
            self.strategies_dir = ROOT / self.strategies_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self

    @property
    def events_dir(self) -> Path:
        return self.data_dir / "events"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "solmeme.db"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"


def load_settings(path: Path | None = None) -> Settings:
    path = path or Path(os.getenv("SOLMEME_SETTINGS", ROOT / "config" / "settings.yaml"))
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    raw = raw or {}
    if os.getenv("SOLMEME_DATA_DIR"):
        raw["data_dir"] = os.getenv("SOLMEME_DATA_DIR")
    return Settings.model_validate(raw).resolve()


@lru_cache(maxsize=1)
def settings() -> Settings:
    return load_settings()


def secret(name: str) -> str:
    return (os.getenv(name) or "").strip()


def upsert_env(path: Path, updates: dict[str, str]) -> list[str]:
    """Write API keys into a dotenv file. Empty values are ignored, so Enter keeps the saved key."""
    written = []
    clean: dict[str, str] = {}
    for key, value in updates.items():
        value = (value or "").strip()
        if not value:
            continue
        if "\n" in value or "\r" in value:
            raise ValueError(f"{key} contains a line break")
        clean[key] = value
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in line:
            out.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in clean:
            out.append(f"{key}={clean[key]}")
            seen.add(key)
            written.append(key)
        else:
            out.append(line)
    for key, value in clean.items():
        if key not in seen:
            if out and out[-1] != "":
                out.append("")
            out.append(f"{key}={value}")
            written.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    return written
