# SolMeme-Trader

A Solana meme-coin **paper-trading lab**. It records the live market from PumpPortal, GMGN and DexScreener. It runs many strategy versions side by side using realistic simulated fills. It finds which rules and patterns actually make money. When it's worth the tokens, it asks Claude to diagnose mistakes and propose fixes. Every fix must beat the current version on data the AI never saw before it's adopted.

No real money is involved. The execution layer is an interface (`sim/fills.py`, `sim/engine.py`), so a live executor can be added later behind a kill-switch.

## Quick start (no API keys needed)

```bash
uv venv && uv pip install -e ".[dev]"
solmeme setup                                          # saves API keys to .env (all optional)

# 1. Try everything on a synthetic market (kept separate in data-demo/)
solmeme --data-dir data-demo synth --tokens 700 --hours 8
solmeme --data-dir data-demo backtest --days 1         # leaderboard of all strategies
solmeme --data-dir data-demo scan --days 1             # what predicts a 2x, across all tokens
solmeme --data-dir data-demo optimize dex_curve_momentum --days 1
solmeme --data-dir data-demo review --dry-run --days 1 # see triage + the exact evidence the AI would get

# 2. Real market
solmeme check                                          # test every source and key
solmeme run                                            # live paper trading (+ scheduled AI cycles)
solmeme report                                         # data/reports/YYYY-MM-DD.md
```

Run `solmeme run` on an always-on machine, such as the homelab or Docker. If the machine sleeps, the recording has gaps. On restart, the bot replays the last 45 minutes of its own log, so open paper positions are restored exactly.

## How the AI is kept cheap

Claude is **never** in the trading path. Every buy and sell decision is hard-coded and deterministic, so it costs nothing and can be replayed. There are four layers:

1. **Global prefilter** (`prefilter:` in `config/settings.yaml`) drops these tokens before any strategy looks at them:
   - honeypots, blacklisted tokens and high GMGN rug ratio
   - tokens where the dev sold more than 80%
   - a single wallet holding more than 50%
   - a price already more than 85% off its peak
   - stale prices
2. **Strategy rules** (`strategies/*.yaml`) are plain feature thresholds such as `unique_buyers_60s >= 12`.
3. **Deterministic triage** (`solmeme/ai/triage.py`) runs before every review and computes statistics for free:
   - which entry features separate winners from losers (AUC)
   - which rules block tokens that later ran 2x
   - how often the token ran 2x after we sold
   - rug-hit rate and fee drag

   **No AI call** is made if:
   - there are fewer than 15 trades,
   - the strategy is healthy and nothing is losing money, or
   - the findings are the same as at the last review of that version.
4. **Claude review**, only when triage finds something. The flags go at the top of the prompt. The large static part of the prompt (feature dictionary and rules) is prompt-cached. There's a daily dollar cap (`ai.daily_budget_usd`), and every call is logged in the `llm_usage` table.

The optimizer (`solmeme optimize`) is a free, non-AI way to tune parameters. Its output goes through the same gates.

## The improvement loop

```
recorded events ──► evidence window (older 70%) ──► replay champion ──► triage ──► Claude review ──► ≤3 proposals
                └─► holdout window (newest 30%, AI never sees it) ──► challenger must beat champion ──┐
live paper trading ◄── challenger runs next to champion ◄─────────────────────────────────────────────┘
        └─► shadow gate: ≥30 trades, bootstrap CI of PnL difference > 0 ──► promote   (else reject → lesson)
```

Proposals are limited to structured changes:
- change a parameter within its bounds
- add or remove a rule, or change a rule's threshold
- change exits or the take-profit ladder

Locked safety rules, position sizing and the universe can never be changed by the AI. Rejected ideas are remembered for 14 days so they aren't proposed again. To roll back, run `solmeme rollback <strategy>`.

## Data sources (as of Oct 2026)

| Source | Cost | What it gives | Notes |
|---|---|---|---|
| PumpPortal ws | free | every new launch + migration | **Per-token trade streams need `PUMPPORTAL_API_KEY` funded with ≥0.02 SOL**, billed about 0.01 SOL per 10k events. Capped by `record.trade_stream_daily_events` and given to the highest-priority tokens. Without a key, the bot runs on snapshots. |
| GMGN OpenAPI | free key ([gmgn.ai/ai](https://gmgn.ai/ai)) | trenches (New/Almost/Migrated), holders, top10, smart-money/KOL counts, rug ratio, bundlers, KOL and smart-money trade feeds, token security | Small rate limit; uses the official API only, no scraping |
| DexScreener | free | price, 5-min buys/sells/volume, liquidity for up to 600 watched tokens every 5s, paid boosts | Indexes pump.fun curve tokens too |
| CoinGecko / Jupiter | free | SOL/USD | Jupiter is used if `JUPITER_API_KEY` is set |
| Rugcheck | free | risk score | off by default |
| Axiom, fomo.family, j7tracker | free (your login) | Axiom Pulse token stats, fomo top-trader trades (free KOL feed), j7 tweets that contain a token address | No official API. A real browser that you log into listens to the site's own traffic. See below. |

### Axiom, fomo.family and j7tracker (browser adapters)

These sites have no data API. The bot opens a real Chromium with a saved login and reads the websocket and API traffic the web app already receives, rather than scraping HTML.

```bash
uv pip install -e ".[browser]" && python -m playwright install chromium
solmeme login fomo          # a browser window opens: log in, then close it (session is saved)
# then set sources.fomo.enabled: true in config/settings.yaml and run `solmeme run`
```

- **Use throwaway accounts.** Automated access is against these sites' terms, so the worst case is a banned account.
- **Never connect a funded wallet or paste a private key** into these browser profiles. Fake "Axiom bot" repos are a known key-stealing trap.
- **For j7tracker, only use j7tracker.io.** The .com domain is flagged as phishing.
- **Payload formats come from community SDKs**, not from captured traffic. Unrecognised messages are saved to `data/browser/<site>/samples.jsonl` so the parsers can be fixed against real data.

Tracked wallets (KOLs you want to follow) go in `sources.tracked_wallets` and are polled through GMGN.

**Copy your keys:** `GMGN_API_KEY` and `ANTHROPIC_API_KEY` already exist in `../PaperTrader/.env`. Put them in this project's `.env`.

## Strategies

| id | data needed | idea |
|---|---|---|
| `dex_curve_momentum` | free | bonding-curve tokens with strong 5-minute DexScreener buy pressure |
| `near_graduation` | free | curve tokens at 75–97% bonding that are still rising |
| `migration_momentum` | free | freshly migrated tokens with buy pressure and real liquidity |
| `trench_quality` | GMGN key | GMGN "New" tokens with healthy holders and smart money (an Axiom-Pulse-style filter) |
| `kol_follow` | GMGN key | at least 2 KOL/smart wallets bought recently; exit when one sells |
| `curve_momentum` | PumpPortal trade stream | early organic demand: distinct buyers, buy/sell ratio, no dev sells, low concentration |
| `launch_sniper` | PumpPortal trade stream (free launch feed is enough to see tokens; flow features need the trade stream) | scores a launch ~12s in, paper-buys, labels 2x-before-50%, updates its own weights |

To add one, drop a YAML file in `strategies/`. The rule vocabulary (about 90 features) is in `solmeme/features/registry.py`. Run `python -c "from solmeme.features.registry import dictionary_text; print(dictionary_text())"` to list them.

## Token sniper

Paper only. It never sends a transaction.

`solmeme snipe` runs just the sniper. `solmeme snipe --replay --days 1` learns from the event log. `solmeme snipe-report` shows which feature bins are working and which are not.

Learning is online logistic weights plus bin win rates, not an LLM in the buy path. Explore occasionally paper-buys a near-miss so it doesn't only learn from its own buys.

## Paper fills are pessimistic on purpose

Every simulated fill includes:
- **Latency:** 0.4–1.5s from signal to fill, priced against the market state at landing.
- **Exact pump.fun curve math**, with the 1.25% curve fee.
- **Tiered PumpSwap AMM fees** after migration.
- **Slippage-limit failures.**
- **Network and platform costs:** priority fee on every attempt, Jito tip, 0.5% platform fee, and token-account rent.
- **Random failures and MEV:** 10% of transactions are dropped, and 15% of buys are sandwiched.

Paper results that survive all that have a real chance. Expect live results to still be worse.

## Layout

```
solmeme/core      events, event log (gz JSONL per hour), SQLite store, clock
solmeme/sources   pumpportal, gmgn, dexscreener, misc (SOL price, rugcheck, stubs), watchlist
solmeme/features  token state (trade stream or snapshots), feature registry, rug veto
solmeme/strategy  YAML spec, rule DSL, guarded diffs
solmeme/sim       curve/AMM math, fill model, portfolio, deterministic engine, replay, live loop
solmeme/sniper    launch score, paper buy, online learner (what is working / what is not)
solmeme/lab       metrics, pattern mining, universe scan, optuna optimizer, synthetic market
solmeme/ai        Claude client, triage, prompt, evidence pack, reviewer, promoter (gates/lessons/rollback)
```

Tests: `pytest`. They cover curve math, fills, the rule DSL and guardrails, replay determinism, strategy isolation, no look-ahead, the prefilter, triage, the promotion gates, and a full review cycle with a fake LLM.
