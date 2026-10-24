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
solmeme run                                            # live paper trading, rich log
solmeme run --ui tui                                   # same session, Bubble Tea full-screen UI
solmeme run --ui web                                   # same session at http://127.0.0.1:8787
./start web 50                                         # fresh run: a new $50 wallet, nothing carried over
solmeme report                                         # data/reports/YYYY-MM-DD.md
solmeme session --every 15 --ai claude                 # in a 2nd terminal: report on the running bot every 15 min
```

The website (Overview, Swarm, Market, Sources) is built from `webapp/` (React, Tailwind, shadcn/ui, Recharts) into `web/`, which the bot serves as plain files, so running the bot needs no Node. After changing it: `cd webapp && npm install && npm run build`. `npm run dev` serves it with hot reload against a running bot (`SOLMEME_API=http://127.0.0.1:8787 npm run dev`). Set `SOLMEME_UI_HOST=0.0.0.0` to serve the site to your LAN (`SOLMEME_UI_PORT` picks the port).

`solmeme run` is the normal rich log. `solmeme run --ui tui` (or `./run tui`) is the same session in a full-screen Bubble Tea UI; the first launch builds `tui/` and needs Go installed. `solmeme run --ui web` (or `./run web`) serves that session at http://127.0.0.1:8787. All three are paper only.

Run `solmeme run` on an always-on machine, such as the homelab or Docker. If the machine sleeps, the recording has gaps. On restart it loads the last 45 minutes of its own log as market data and starts a fresh wallet. Pass `--resume` to replay those 45 minutes of trades as well, which restores open paper positions exactly. Only one session can run at a time; a second one exits with the first one's pid.

`./start` is the fresh-wallet version. Every run begins as if you just put money in: `portfolio.starting_usd` ($50) converted to SOL at the current price, one wallet shared by every strategy, and no positions or trades carried over. It still loads the last 45 minutes of market data so the strategies have price history from the first second. `./start 75` sets the amount, `--goal 200` the target, and `./start web` / `./start tui` pick the UI.

`solmeme session` reports on the running bot from another terminal: wallet (with its high and low), trades closed in the window with their peak and dip, open positions, data sources, and problems it detects on its own (a stalled feed, a quiet source, stops filling far past the stop, a losing streak, a drawdown, low cash, no buys, a second bot). The bot writes `data/session/live.json` every minute for it. Reports go to `data/reports/session/<run>/`. `--every 15` repeats it. `--ai claude` adds a short note from Claude through the Claude Code CLI (`claude -p`, your Claude login, no tools, about $0.015 a note), and `--ai local` uses an Ollama model (`ai.session_review.local_model`). The AI is asked only when trades closed or a new problem appeared since its last note, and it counts against `ai.daily_budget_usd`.

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

Locked safety rules, position sizing and the universe can never be changed by the AI. `solmeme demote <id>` moves a losing champion off the shared wallet; it keeps trading as a trial on its own wallet (undo with `solmeme promote`). On Oct 8, `dex_curve_momentum`, `near_graduation`, `migration_momentum`, `trench_quality` and `kol_follow` were demoted after losing in every replay period and live. Rejected ideas are remembered for 14 days so they aren't proposed again. To roll back, run `solmeme rollback <strategy>`.

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

`solmeme trace <address>` reads that wallet's public SOL and token transfers, lists who funded it and who it paid, and names known programs and pass-through patterns from the chain itself. It also reads the largest counterparties one step further and flags wallets that share a funder (`--hops 1` stops at the direct links). It sends no transactions. `HELIUS_API_KEY` includes token-account transfers; without it the command uses a public RPC and can miss token-only sends. The People panel uses the fomo leaderboard and the trades that leaderboard records.

**Copy your keys:** `GMGN_API_KEY` and `ANTHROPIC_API_KEY` already exist in `../PaperTrader/.env`. Put them in this project's `.env`.

## Strategies

| id | data needed | idea |
|---|---|---|
| `holder_dip` | GMGN key | **The only strategy positive in every tested period (Oct 8 analysis).** Curve tokens (pump.fun or Meteora) 30s–15 min old with 40+ GMGN holders and $8k+ liquidity, bought 20–70% below their high; take profit at 3x, stop at −25%, no trailing stop. Champion since Oct 8. |
| `dex_curve_momentum` | free | bonding-curve tokens with strong 5-minute DexScreener buy pressure |
| `near_graduation` | free | curve tokens at 75–97% bonding that are still rising |
| `migration_momentum` | free | freshly migrated tokens with buy pressure and real liquidity |
| `trench_quality` | GMGN key | GMGN "New" tokens with healthy holders and smart money (an Axiom-Pulse-style filter) |
| `kol_follow` | GMGN key | at least 2 KOL/smart wallets bought recently; exit when one sells |
| `curve_momentum` | PumpPortal trade stream | early organic demand: distinct buyers, buy/sell ratio, no dev sells, low concentration |
| `launch_sniper` | PumpPortal trade stream (free launch feed is enough to see tokens; flow features need the trade stream) | scores a launch ~12s in, paper-buys, labels 2x-before-50%, updates its own weights |

### Trials

A strategy in `strategies/trials/` starts as a **trial**: it trades every run on its own paper copy of the starting wallet (like a challenger), so it builds a track record without touching the shared wallet. Editing the file replaces the running trial version. `solmeme promote <id> <version>` moves a trial that earns onto the shared wallet; `solmeme kill <id> <version>` stops one. Three trials ship for established coins (any age, $100k+ liquidity) and one for Meteora's bonding curve, none of them proven yet:

| id | idea |
|---|---|
| `established_breakout` | +4% in 5 minutes on heavy volume |
| `established_trend` | up 20%+ over the hour and still rising |
| `established_dip` | down 8%+ over the hour and turning up |
| `dbc_momentum` | Meteora Dynamic Bonding Curve launches (Meteora, Bags, Believe, Jupiter Studio) with 5-minute buy pressure. The #2 launchpad by new tokens in our data (about 1,800 a day against pump.fun's ~11,000; LetsBonk and Boop are near zero). Paper fills use a flat, pessimistic 2% curve fee (`fills.other_curve_fee_pct`) because each DBC launchpad sets its own. |

To add one, drop a YAML file in `strategies/` (or `strategies/trials/` to try it first). The rule vocabulary (about 90 features) is in `solmeme/features/registry.py`. Run `python -c "from solmeme.features.registry import dictionary_text; print(dictionary_text())"` to list them.

### Bet sizing

Every buy is `portfolio.position_pct` (15%) of the current value of the wallet it trades from: the champions' shared wallet, or a challenger's, trial's or swarm agent's own wallet. Bets shrink after losses and grow after wins, so a wallet that is down never freezes for lack of cash. `portfolio.reserve_sol` (0.01 SOL) is always kept for sell fees. Remove `position_pct` to go back to each strategy's fixed `sizing.sol`.

### Strategy swarm

`solmeme run` keeps a swarm of up to 16 strategy variants trading next to the champions, each on its own $50 paper wallet. Once an hour it runs a generation in a low-priority background process (`logs/evolve.log`). No LLM is involved, so it costs nothing but CPU:

Agents keep their wallet across bot restarts: it is rebuilt from their own closed trades (positions open at shutdown are dropped).

1. **Judge.** An agent dies when its wallet falls below 25% of the start, when it is in the bottom quarter with a losing record after 8 live trades, or when it sits idle for 12 hours while the rest trade.
2. **Learn.** Losing trades become *lessons*: the entry condition whose cut would have removed the most losing SOL (for example "don't buy when `dex_price_chg_h1 > 340`"). Each mutation's outcome is credited to its kind (screened out, born, killed). Kinds that fail three times without ever helping are almost never tried again, and a dead genome is never recreated.
3. **Breed.** Parents are picked by tournament (live score once an agent has trades, replay score before). Two agents of the same strategy can be crossed, then 1–3 mutations are applied: params, rule thresholds, new rules, lessons as `none` rules, exits, the take-profit ladder. Mutations go through `strategy/diff.py`, so the AI's guardrails apply too: params stay in bounds, locked rules stay, the universe never changes. The one extra gene is **bet size** (5–35% of the agent's wallet per buy), which only the swarm can change, never the AI.
4. **Screen.** Children are replayed over the last 24 hours in 3 walk-forward folds. Only children at least as good as their parent and the worst survivor go live.
5. **Promote.** An agent that beats its strategy's champion on live trades (30+ trades, bootstrap CI > 0) becomes the champion on the shared wallet.

`solmeme swarm` shows the leaderboard, and `data/reports/evolution/latest.md` shows the last generation: who died and why, who was born from what, the lessons, and what each kind of mutation has done so far. `solmeme evolve` runs a generation by hand. Settings live under `evolution:` in `config/settings.yaml`.

### Ban list

`<data>/banned_wallets.json` holds pump-and-dump operators. The live bot skips any token whose creator is banned or that a banned wallet buys (global prefilter rule `banned_wallets >= 1`, reloaded every minute). Each swarm generation scans the recorded trades for new suspects: coordinated clusters (wallets that keep buying the same tokens within 30s of each other) and rug creators (a launch that pumped 4x+ and then lost 90% within 5 minutes). With the council on, the judges vote on each candidate from its evidence; rejected candidates aren't proposed again for a day. `solmeme ban list|add|remove` manages it by hand, and the website lists it under Market. It is a trade-off: these operators run many of the tokens that pump, so the list blocks some winners along with the dumps (the Oct 8 seed list would have blocked the +198% Gunner trade).

### Council

LLM judges (`council:` in `config/settings.yaml`) look at why coins made money and turn that into entry rules for the swarm to test. They run inside each swarm generation, for the 3 busiest strategies, and never touch the buy/sell path:

1. **Explain.** Each judge sees winners against losers (entry features, the best coins, earlier ideas and how they did live) and proposes up to 2 rules, such as "buy only when `gmgn_holders >= 80`" or "don't buy when `top10_pct >= 45`", with a reason.
2. **Check.** Each rule is replayed on the same trades. Rules that would not have saved SOL, that block too little, or that keep under 30% of trades are dropped before any vote.
3. **Vote.** The judges vote on the survivors. Votes are weighted by confidence and by each judge's record.
4. **Test.** Approved ideas become a mutation kind: the swarm breeds children that carry them, and those trade live on their own wallets.
5. **Judge.** After 20 live trades by its carriers (or 2 deaths), each idea is marked `working` or `failed`. Failed ideas stop being bred, and the judges who proposed or backed them lose voting weight.

Judges can be local Ollama models (`kind: ollama`, thinking turned off) or any OpenAI-compatible router (`kind: openai`, key from `.env`). A judge without its key or model sits out. `solmeme council` lists the judges and ideas, and the website shows them under Swarm → Council.

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
solmeme/lab       metrics, pattern mining, universe scan, wallet trace, optuna optimizer, synthetic market
solmeme/ai        Claude client, triage, prompt, evidence pack, reviewer, promoter (gates/lessons/rollback)
```

Tests: `pytest`. They cover curve math, fills, the rule DSL and guardrails, replay determinism, strategy isolation, no look-ahead, the prefilter, triage, the promotion gates, and a full review cycle with a fake LLM.
