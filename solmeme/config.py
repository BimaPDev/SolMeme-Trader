"""Settings: config/settings.yaml for everything tunable, .env for secrets."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

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
    rate_per_s: float = 1.0           # shared by every GMGN client in the process (feeds and scans)
    trenches_poll_s: float = 10
    trench_limit: int = 80
    trench_types: list[str] = ["new_creation", "near_completion", "completed"]
    wallet_feeds: list[str] = ["smartmoney", "kol"]
    wallet_feed_poll_s: float = 15
    tracked_wallet_poll_s: float = 30
    security_max_per_min: int = 10


class DexCfg(BaseModel):
    enabled: bool = True
    boosts_poll_s: float = 60
    watch_poll_s: float = 5
    max_requests_per_min: int = 240


class SolStreamCfg(BaseModel):
    enabled: bool = True          # needs SOLANASTREAMING_API_KEY; skipped otherwise
    url: str = "wss://api.solanastreaming.com"
    max_mints: int = 25           # held tokens first, then the top of the watchlist
    wallets: bool = True          # also stream swaps by tracked wallets and top whales
    daily_messages: int = 0       # 0 = no cap; when reached, only held tokens stay subscribed


class GeckoCfg(BaseModel):
    enabled: bool = True          # GeckoTerminal public API, no key, 30 calls/min
    trending_poll_s: float = 120
    watch_poll_s: float = 20      # backup prices for held tokens when DexScreener is stale
    max_requests_per_min: int = 20


class BundleTraceCfg(BaseModel):
    enabled: bool = True          # linked/fresh wallets among top holders (sources/bundletrace.py), idea from crawl-hive
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    max_requests_per_s: float = 1.5
    poll_s: float = 10
    top_n: int = 8                # the coins nearest a buy
    max_wallets: int = 12
    recheck_s: float = 600


class ChainCheckCfg(BaseModel):
    enabled: bool = True          # our own rug check from public Solana RPC (sources/chaincheck.py), shadow only
    rpc_urls: list[str] = ["https://api.mainnet-beta.solana.com"]   # HELIUS_API_KEY in .env goes first (and adds holders)
    max_requests_per_s: float = 2.0
    poll_s: float = 15
    top_n: int = 12               # the coins nearest a buy: top of the watchlist and held ones
    per_poll: int = 3             # 1 call each (4 with holders)
    recheck_s: float = 120        # holder shares move; mint flags are read once
    top1_max_pct: float = 50
    top10_max_pct: float = 80


class PumpFunCfg(BaseModel):
    enabled: bool = True          # pump.fun's own real-time trade feed (sources/pumpfun_ws.py), free
    max_coins: int = 150          # keep trades for this many coins from the top of the watchlist (held first)
    daily_events: int = 3_000_000 # recorded trades a day (0 = no cap); after it only held/asked coins stay
    min_sol: float = 0.0005       # bots' dust trades (0.0001 SOL) only inflate trade counts


class PumpFunSocialCfg(BaseModel):
    enabled: bool = True          # pump.fun's Trending board and Hot callouts (sources/pumpfun_social.py)
    trending_poll_s: float = 60
    trending_limit: int = 50
    callouts_poll_s: float = 60
    feed_size: int = 100
    leaders_poll_s: float = 900   # daily/weekly PnL leaderboard: who the callers are


class NewsCfg(BaseModel):
    enabled: bool = True          # Google News + Google Trends RSS (+ Reddit), free, no key
    poll_s: float = 300
    reddit: bool = True


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
    solstream: SolStreamCfg = SolStreamCfg()
    gecko: GeckoCfg = GeckoCfg()
    chaincheck: ChainCheckCfg = ChainCheckCfg()
    bundletrace: BundleTraceCfg = BundleTraceCfg()
    pumpfun: PumpFunCfg = PumpFunCfg()
    pumpfun_social: PumpFunSocialCfg = PumpFunSocialCfg()
    news: NewsCfg = NewsCfg()
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
    curve_fee_pct: float = 1.25                  # pump.fun curve
    # Other launchpad curves set their own fees (Meteora DBC: per launchpad, often with an
    # anti-sniper schedule right after launch), so they get a pessimistic flat fee unless listed.
    other_curve_fee_pct: float = 2.0
    curve_fee_by_pool: dict[str, float] = {}
    amm_fee_tiers: list[tuple[float, float]] = [(420, 1.25), (2000, 0.95), (20000, 0.60), (98240, 0.45), (1e12, 0.30)]
    sandwich_prob: float = 0.15
    sandwich_extra_pct: float = 3.0
    # with no MEV-protection tip (jito_tip_sol 0) sandwich bots get the buy more often and take more,
    # up to the slippage limit; both paper fills and the live dry run use these then
    sandwich_prob_unprotected: float = 0.30
    sandwich_extra_unprotected_pct: float = 6.0
    stale_exit_haircut_pct: float = 30
    # Correct paper fills with the gaps the live dry run measured (sim/calibration.py); the correction
    # grows with the sample: weight = n / (n + calibration_prior_n).
    calibrate_from_live: bool = True
    calibration_prior_n: int = 30

    def curve_fee(self, pool: str) -> float:
        if pool in self.curve_fee_by_pool:
            return self.curve_fee_by_pool[pool]
        return self.curve_fee_pct if pool == "pump" else self.other_curve_fee_pct

    def amm_fee_pct(self, mcap_sol: float | None) -> float:
        m = mcap_sol or 0.0
        for cap, fee in self.amm_fee_tiers:
            if m <= cap:
                return fee
        return self.amm_fee_tiers[-1][1]


class PortfolioCfg(BaseModel):
    starting_sol: float = 10.0
    starting_usd: float | None = None   # if set, live runs convert this to starting_sol at the SOL price
    goal_usd: float | None = None       # wallet target shown in the UIs
    shared_wallet: bool = False         # live runs: every strategy spends from one wallet
    max_position_sol: float = 1.0
    position_pct: float | None = None   # buy this % of the wallet's current value (None: each spec's fixed sol)
    reserve_sol: float = 0.05           # cash never spent on buys (left for sell fees)
    entry_settle_s: float = 0.0         # live: no new buys this long after the session starts
    keep_wallet: bool = False           # live: the champions' wallet carries over restarts (open positions at market)
    one_position_per_token: bool = False  # shared wallet: only one champion may hold a given token at a time
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


class SessionReviewCfg(BaseModel):
    every_min: float = 15
    provider: str = "claude"            # claude (Claude Code CLI, your Claude login) | local (Ollama) | none
    claude_model: str = "sonnet"
    local_url: str = "http://localhost:11434/v1"
    local_model: str = "qwen2.5:1.5b"
    timeout_s: float = 300


class AiCfg(BaseModel):
    triage: TriageCfg = TriageCfg()
    session_review: SessionReviewCfg = SessionReviewCfg()
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


class EvolutionCfg(BaseModel):
    """Strategy swarm (solmeme/lab/evolve.py). Deterministic, no LLM: costs nothing but CPU."""
    enabled: bool = True
    every_min: float = 60                # `run` starts a generation this often (low priority subprocess)
    population: int = 16                 # live swarm agents, each on its own paper wallet
    offspring_per_slot: int = 6          # children screened by replay per free slot
    lesson_fanout: bool = True           # each species' top new lesson is also tried at 3 strengths on its best parent
    tournament: int = 2                  # parents: best of this many random picks
    max_species_share: float = 0.5       # no strategy may hold more than this share of the swarm
    species_exclude: list[str] = ["launch_sniper", "curve_momentum"]
    window_h: float = 24                 # replay window used to score and screen
    folds: int = 3                       # walk-forward folds inside the window (median score)
    min_replay_trades: int = 5
    shrink_trades: int = 20              # replay scores shrink toward 0 below this many trades (luck guard)
    min_live_trades: int = 8             # live results start to judge an agent after this many trades
    bust_frac: float = 0.25              # dead when its wallet is below this share of the start
    kill_bottom_frac: float = 0.25       # after min_live_trades: this share at the bottom dies if losing
    kill_min_trades: int = 4             # from this many live trades on, an agent that is losing with
    kill_confidence: float = 0.8         # this confidence (bootstrap of its mean trade %) dies
    idle_kill_h: float = 12              # dead when it has not traded this long while the swarm did
    min_free_slots: int = 2              # turnover: when a full swarm would lose nobody, the weakest judged agents
                                         # (never the top half) retire so new children can be tried
    max_mutations: int = 3
    crossover_prob: float = 0.3
    trace_whales: bool = True            # public-RPC check of new whales' money for banned counterparties
    elite_children: int = 2              # guaranteed children per live winner each generation (lab/winners.py)
    elite_margin: float = 5.0            # a winner's child may score this much below it in replay and still go live
    stress_tests: bool = True            # replay winners under worse conditions (lab/winners.py)
    stress_every: int = 3                # every Nth generation, plus whenever a winner has never been tested
    stress_window_h: float = 12          # how much recent history the stress replays cover
    min_robustness: float = 0.6          # share of stress scenarios a swarm agent must survive to be promoted
    auto_promote: bool = True            # live winner over its champion (bootstrap CI > 0) takes the shared wallet
    seed: int | None = None


class CouncilMember(BaseModel):
    name: str
    kind: Literal["ollama", "openai"] = "openai"   # ollama: native API (thinking off); openai: any compatible router
    base_url: str
    model: str = ""
    key_env: str = ""                    # env var holding the API key; the judge sits out while it is unset
    timeout_s: float = 300
    keep_alive: str = "10m"              # ollama: keep the model loaded between calls of one sitting
    temperature: float = 0.2
    enabled: bool = True
    fallback: bool = False               # backup judge: only asked questions no other judge answered
    # longest prompt sent (characters, ~3.5 per token). Local models have small context windows
    # (qwen27b on Ollama: 4096 tokens); a longer prompt is trimmed in the middle, keeping the instructions.
    max_prompt_chars: int = 0            # 0 = no limit; ollama judges default to MAX_LOCAL_PROMPT_CHARS


class CouncilCfg(BaseModel):
    """LLM judges that explain winning coins as entry rules for the swarm to test (solmeme/ai/council.py)."""
    enabled: bool = False
    members: list[CouncilMember] = []
    max_species: int = 3                 # strategies studied per generation (busiest first)
    min_trades: int = 20                 # a strategy needs this many recent trades to be studied
    max_ideas_per_member: int = 2
    approve_score: float = 0.5           # reputation-weighted, confidence-weighted share of yes votes
    judge_min_trades: int = 20           # carriers' live trades before an idea is called working/failed
    max_seconds: float = 1200            # time budget per sitting (local models can be slow to load)
    # Cursor judges spend a paid plan: each sitting checks the real balance and allows itself an even
    # share per day (remaining / days to the billing reset). Past today's share they sit out until tomorrow.
    cursor_pacing: bool = True
    auto_picks: bool = False             # whale clusters go to the council; a clear Buy is bought by council_pick
    picks_per_day: int = 40              # ~3 Cursor calls each
    early_picks: bool = True             # fast lane: coins under 3 min old with 2+ early signals go to the council
    early_picks_per_day: int = 60


class SniperCfg(BaseModel):
    """Paper-only launch sniper. It never sends a transaction."""
    enabled: bool = True
    decision_s: float = 12
    horizon_s: float = 600
    explore: bool = True


class OverseerCfg(BaseModel):
    """sim/overseer.py: the last check before the live wallet buys; paper is never stopped."""
    enabled: bool = True
    max_data_age_s: float = 120          # live trade feed or price data quiet this long: no new live buys
    day_loss_pct: float = 20             # live wallet down this much in 24 h: no new buys until it recovers
    max_exposure_pct: float = 70         # at most this share of the wallet in open positions
    council_loss_streak: int = 4         # the council's last N live picks all lost: council_pick buys pause


class DryRunCfg(BaseModel):
    """Live dry run: a second wallet that mirrors every champion fill at real executable prices
    (Jupiter quotes at the moment of the fill, our own price impact included). Nothing is ever sent."""
    enabled: bool = False
    start_usd: float = 50.0
    quote_url: str = "https://lite-api.jup.ag/swap/v1/quote"
    token_url: str = "https://lite-api.jup.ag/tokens/v2/search"
    slippage_tolerance_pct: float = 25.0     # a buy whose real price is this much worse than the decision price fails
    platform_fee_pct: float = 0.5            # execution venue fee per leg (PumpPortal local 0.5%); 0 for plain Jupiter
    sell_retries: int = 6                    # unsellable (no route): retry this many times ...
    sell_retry_s: float = 10.0               # ... this far apart, then the position counts as stuck at 0
    mark_every_s: float = 60.0               # open positions re-valued with a real sell quote this often
    loss_limit_pct: float = 50.0             # the kill switch a real wallet would have: no new buys below this loss
    council_review: bool = False             # the council judges every live buy right after it; a clear Sell sells it
    council_reviews_per_day: int = 30        # ~3 Cursor calls each
    timeout_s: float = 6.0
    # The watcher (Oct 11): the live wallet's own guard, on its own real prices, separate from paper. Before a buy:
    # paired with SOL, not already falling on pump.fun's own chart. While holding (every mark): the strategy's stop
    # and time limit on the REAL value (paper's price can go stale and never fire them, as on Zoroark), the creator
    # dumping. Deterministic: no AI call.
    watcher: bool = True
    watcher_hard_stop_pct: float = 40.0      # when the strategy has no stop
    watcher_hold_grace_s: float = 120.0      # past the strategy's max hold by this much: sell
    watcher_ath_drop_pct: float = 10.0       # don't buy a coin already this far under its pump.fun high
    watcher_dev_sold_pct: float = 50.0       # the creator has sold this much of their peak bag: get out
    watcher_data_gap_pct: float = 30.0       # real price this much under the decision price: the strategy's data is wrong, skip


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
    evolution: EvolutionCfg = EvolutionCfg()
    council: CouncilCfg = CouncilCfg()
    dryrun: DryRunCfg = DryRunCfg()
    overseer: OverseerCfg = OverseerCfg()
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


_env_file: tuple[float, dict[str, str]] = (-1.0, {})


def secret(name: str) -> str:
    """An API key: the process environment first, else .env as it is on disk now, re-read when the
    file changes, so a key added while the bot runs counts without a restart (the dashboard too)."""
    global _env_file
    value = os.getenv(name)
    if value:
        return value.strip()
    path = ROOT / ".env"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    if mtime != _env_file[0]:
        from dotenv import dotenv_values
        _env_file = (mtime, {k: v for k, v in dotenv_values(path).items() if v})
    return (_env_file[1].get(name) or "").strip()


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
