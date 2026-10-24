import { useEffect, useRef, useState } from "react"

export type Wallet = {
  strategies: number
  shared: boolean
  best: string | null
  start_sol: number
  start_usd: number | null
  balance_sol: number
  balance_usd: number | null
  pnl_sol: number
  pnl_usd: number | null
  pnl_pct: number | null
  goal_usd: number | null
  goal_pct: number | null
  reached: string[]
  locked_sol?: number | null
  locked_usd?: number | null
  open_sol?: number | null
  open_usd?: number | null
  range: { unit: string; high: number; high_ts: number | null; low: number; low_ts: number | null; from_high_pct: number | null } | null
}

export type StrategyRow = {
  key: string
  role: string
  open: number
  closed: number
  win_pct: number
  pnl_sol: number
  made_usd: number | null
  cash_sol: number | null
  balance_sol: number
  balance_usd: number | null
  in_wallet: boolean
}

export type Source = { name: string; status: string; events: number; errors: number; note: string }
export type Position = {
  coin: string; strategy: string; status: string; bought: string; entry: string; sold: string; mark: string; made: string
  mint: string; invested_sol: number; worth_sol: number; invested_usd: number | null; worth_usd: number | null
  key?: string; role?: string
}
export type Token = { symbol: string; mint: string; mint_short: string; phase: string; detail: string; age: string; ts: number; source: string }
export type BoardRow = { symbol: string; mcap_usd: number | null; holders?: number | null; bonding_pct?: number | null; volume_usd?: number | null; mint: string }
export type Card = { verdict: string; text: string }

export type DryRunTrade = {
  mint: string; symbol: string; key: string; entry_ts: number; exit_ts: number; size_sol: number; proceeds_sol: number
  pnl_sol: number; pnl_pct: number | null; fees_sol: number; entry_price: number | null; exit_price: number | null
  paper_entry_price: number | null; paper_exit_price: number | null; paper_pnl_pct: number | null
  buy_impact_pct: number | null; sell_impact_pct: number | null; route: string; exit_reason: string
  status: "closed" | "stuck" | "missed"; note: string
  path?: [number, number][]
}

/** The live dry run: champion fills priced from real Jupiter quotes, on a wallet the size of a real deposit. */
export type DryRunView = {
  enabled: boolean; started_ts: number; start_sol: number; start_usd: number
  equity_sol: number; equity_usd: number | null; cash_sol: number; pnl_pct: number | null; paper_pnl_pct: number | null
  high_sol: number; low_sol: number; trades: number; won: number
  avg_gap_pct: number | null; avg_entry_gap_pct: number | null; avg_buy_impact_pct: number | null
  counts: Record<"buys" | "sells" | "failed_buys" | "slippage" | "no_route" | "no_cash" | "stuck" | "sell_retries" | "quote_errors" | "skipped", number>
  open: { symbol: string; mint: string; key: string; cost_sol: number; value_sol: number; entry_ts: number; marked_ts: number
          impact_pct: number; entry_gap_pct: number | null; sell_fails: number; route: string; path?: [number, number][]
          why?: string; sol_in?: number; proceeds_sol?: number; sold_pct?: number; entry_mcap_usd?: number | null; mcap_usd?: number | null
          peak_value_sol?: number; exit_plan?: string[]
          council?: { verdict?: string; agreement?: number; action?: string; why?: string[] } }[]
  recent: DryRunTrade[]
  tape: LiveFill[]
}

/** One line of the live wallet's own trade log, newest first. */
export type LiveFill = {
  ts: number; side: "buy" | "sell" | "missed" | "sell_failed" | "stuck" | "stopped" | "blocked"; key: string; mint: string; symbol: string
  paper_price: number | null; price?: number; sol?: number; impact_pct?: number; route?: string; why?: string
  frac?: number; partial?: boolean; x?: number | null; pnl_sol?: number | null; paper_pnl_pct?: number | null
  whales?: number | null; size_mult?: number
}

/** The few live-wallet numbers sent with every snapshot (header, sidebar). */
export type DryRunSummary = {
  enabled: boolean; started_ts: number; start_sol: number; start_usd: number
  equity_sol: number; equity_usd: number | null; pnl_pct: number | null; paper_pnl_pct: number | null
  open: number; trades: number; attempt?: number; stopped?: string | null
}

/** /api/live: the live dry-run wallet in full. */
export type LiveView = (DryRunView & {
  started: true; series: ([number, number] | [number, number, number])[]; sol_usd: number | null; now: number
  attempt: number; stopped: { ts: number; equity_sol: number; reason: string } | null; loss_limit_pct: number
  attempts: { attempt: number; started_ts: number; ended_ts: number; start_usd: number; end_usd: number | null
              pnl_pct: number | null; paper_pnl_pct: number | null; trades: number; won: number; ended_by: string }[]
  calibration: { n: number; weight: number; no_route: number; buy_gap_median: number | null; sell_gap_median: number | null; updated: number } | null
  readiness: { id: string; ok: boolean; label: string; detail: string }[]
  history: DryRunTrade[]
}) | { started: false; enabled: boolean; error?: string }

export type Snapshot = {
  paper: boolean
  now: number
  uptime_s: number
  events: number
  sol_usd: number | null
  wallet: Wallet | null
  shared_wallet: boolean
  dryrun?: DryRunSummary | null
  sources: Source[]
  trade_streams: { unavailable: boolean; subscribed: number; events_today: number; budget_exhausted: boolean } | null
  strategies: StrategyRow[]
  positions: Position[]
  tokens: Token[]
  sniper: Record<string, number | string | boolean | null> | null
  boards: {
    trenches_source: string | null
    trending_source: string | null
    trenches: Record<string, BoardRow[]>
    trending: BoardRow[]
    pump: Record<string, BoardRow[]>
    cards: Card[]
  } | null
  people: Card[]
  people_note: string
  feed: string[]
  scan_note: string
  whales?: { address: string; status: string; score: number; win: number | null; median_exit: number | null; round_trips: number; early_good: number; label: string; flag: string; tracked: boolean; history: number[]
    council?: "trust" | "distrust" | "unsure" | null; council_score?: number | null; council_reason?: string; council_ts?: number | null }[] | null
  bans?: { count: number; tokens_blocked: number; recent: { address: string; role: string; source: string; ts: number; reason: string }[] } | null
  swarm_clock: { running: boolean; phase?: string | null; started_ts?: number | null; next_ts: number | null; every_min: number; agents: number } | null
}

export type History = {
  sample_s: number
  wallet: [number, number | null, number | null][]
  runners: Record<string, [number, number | null][]>
}

export type Trade = {
  symbol: string | null
  mint: string
  entry_ts: number
  exit_ts: number
  pnl_sol: number
  pnl_pct: number
  exit_reason: string | null
  hold_s: number | null
  mfe_x: number | null
}

export type Agent = {
  key: string
  species: string
  version: string
  status: string
  gen: number
  born_ts: number
  parent: string | null
  mate: string | null
  ops: string[]
  replay: number | null
  replay_n: number | null
  fitness: number | null
  parent_fitness: number | null
  ended_ts: number | null
  reason: string
  live_n: number
  wins: number
  pnl_sol: number
  pnl_usd: number | null
  equity: number
  bet_pct: number | null
  curve: [number, number][]
  trades: Trade[]
}

export type CouncilMember = {
  name: string; model: string; kind: string; available: boolean; enabled: boolean; missing_key: boolean
  calls: number; ok: number; avg_s: number | null; last_ts: number | null; last_error: string
  reputation: number | null; weight: number; proposed: number; working: number; failed: number
}

export type CouncilIdea = {
  idea_id: string; ts: number; gen: number; strategy_id: string; feature: string; op: string; v: number
  kind: "require" | "avoid"; reason: string; proposed_by: string
  votes: Record<string, { approve: boolean; confidence: number; reason: string }>
  score: number | null; approved: boolean
  in_sample: { n?: number; blocked?: number; kept?: number; saved_sol?: number; winners_lost?: number }
  status: "unhelpful" | "rejected" | "testing" | "working" | "failed"; status_reason: string
  carriers: number; live_trades: number; live_pnl_sol: number; deaths: number
}

export type TokenContribution = { mint: string; symbol: string; trades: number; pnl_sol: number; best_pct: number | null; peak_x: number | null; share_of_profit: number }

export type BestEntry = {
  key: string; status: string; trades: number; win: number; pnl_sol: number; pnl_usd: number | null; equity: number
  curve: [number, number][]; top_token: TokenContribution | null
}

export type AgentProfile = {
  error?: string
  key: string; status: string; note: string; since: number; start_sol: number
  trades_n: number; win: number | null; pnl_sol: number; pnl_usd: number | null; equity: number
  best_trade: number | null; worst_trade: number | null
  curve: [number, number, string?, number?][]
  tokens: TokenContribution[]
  trades: { symbol: string | null; mint: string; entry_ts: number; exit_ts: number; pnl_sol: number; pnl_pct: number; exit_reason: string | null; hold_s: number | null; mfe_x: number | null; size_sol: number | null }[]
  lineage: { version: string; status: string; created_ts: number; note: string; gen: number | null; ops: string[]; mate: string | null; replay: number | null; trades: number; pnl_sol: number }[]
  children: { version: string; status: string; created_ts: number }[]
  spec: {
    description: string
    universe: { pools: string[] | null; stage: string; min_age_s: number; max_age_s: number | null }
    rules: { section: string; id: string; feature: string; op: string; value: unknown; param: string | null; locked: boolean }[]
    exits: { take_profit: [number, number][]; stop_loss_pct: number | null; trailing_pct: number | null; trailing_activate_x: number; max_hold_s: number | null; stale_after_s: number | null; on_rug: boolean; on_dev_sell: boolean; on_kol_sell: boolean; on_migration: string }
    sizing: { pct: number | null; sol: number; max_open: number }
    params: Record<string, { value: number; lo: number; hi: number }>
  }
  changes_vs_parent: string[]
  lifetime?: { trades: number; pnl_sol: number }
  insight?: {
    rank?: number; equity: number; trades: number; win: number
    keepers: { feature: string; op: string; value: number; saves_sol: number; keeps_profit: number; drops: number }[]
    exits: { exit: string; trades: number; pnl_sol: number; avg_sol: number }[]
    genes: { version: string; ops: string[]; trades: number; pnl_sol: number }[]
    hold_win_s: number; hold_loss_s: number; ladder: [number, number][]; stop_loss_pct: number | null; bet_pct: number | null
  } | null
  stress?: {
    robustness: number; live_p_profit: number; live_p10_avg_pct: number; gen: number; ts: number
    scenarios: Record<string, { trades: number; pnl_sol: number; avg_pct: number; passed: boolean }>
  } | null
}

export type Swarm = {
  error?: string
  best?: BestEntry[]
  council?: { enabled: boolean; members: CouncilMember[]; ideas: CouncilIdea[] }
  generation: number
  last_ts: number | null
  config: { population: number; every_min: number; bust_frac: number; min_live_trades: number; kill_bottom_frac: number; idle_kill_h: number; enabled: boolean; position_pct: number | null; kill_min_trades?: number; kill_confidence?: number }
  start_sol: number
  generations: { gen: number; ts: number | null; alive: number; best: number | null; median: number | null; born: number; killed: number; screened_out: number; promoted: number }[]
  species: { id: string; champion: string | null; champion_fitness: number | null; alive: number; dead: number; promoted: number; best: string | null; best_fitness: number | null; live_pnl_sol: number }[]
  agents: Agent[]
  lessons: { strategy_id: string; feature: string; op: string; v: number; saved_sol: number; blocked: number; of: number }[]
  credit: { strategy_id: string; kind: string; n: number; mean_delta: number; weight: number }[]
  events: { ts: number; gen: number; event: string; key: string; reason: string; ops: string[]; fitness: number | null; parent_fitness: number | null }[]
}

/** One line of a council sitting, as it happened (judge answers, the chair's data checks and verdicts). */
export type CouncilMessage = {
  id: number; ts: number; topic: string
  phase: "open" | "explain" | "check" | "vote" | "verdict" | "bans" | "close"
  speaker: string; kind: "asking" | "answer" | "error" | "system"; text: string
  data: { model?: string; seconds?: number; raw?: unknown; evidence?: string; rules?: string[]; approved?: boolean; passed?: boolean }
}

export type CouncilChatView = {
  now: number
  sitting: string
  sittings: { sitting: string; gen: number | null; started: number; last_ts: number; n: number; closed: number; live: boolean }[]
  messages: CouncilMessage[]
  judges: Record<string, { model: string; kind: string; fallback: boolean; enabled: boolean }>
  generation: { gen: number | null; phase: string | null; started: number | null }
  error?: string
}

export type CursorMeter = {
  since: number; calls: number; ok: number; input_tokens: number; output_tokens: number; cache_read: number
  models: { model: string; calls: number; ok: number; avg_s: number | null; input_tokens: number; output_tokens: number; cache_read: number }[]
}

/** Cursor usage: the bridge's own meter, and the plan's real balance when its session cookie is set. */
export type CursorUsage = {
  configured: boolean; error?: string; now?: number
  today?: CursorMeter; last_24h?: CursorMeter; cycle?: CursorMeter; cycle_from?: string
  account?: { configured: boolean; ok?: boolean; error?: string; fetched_at?: number; summary?: Record<string, unknown> }
  judges?: { name: string; model: string; enabled: boolean }[]
}

export type SessionReport = { markdown: string | null; updated: number | null; note: string | null; note_at: number | null; next_at: number | null }

/** Poll a JSON endpoint. Keeps the last good value; `ok` turns false only after repeated failures. */
export function usePoll<T>(url: string, everyMs: number, timeoutMs = 8000) {
  const [data, setData] = useState<T | null>(null)
  const [ok, setOk] = useState(true)
  const [error, setError] = useState("")
  const fails = useRef(0)
  const tickRef = useRef<(force?: boolean) => Promise<void>>(async () => {})

  useEffect(() => {
    let alive = true
    let busy = false                 // per effect run, so a remount (StrictMode, new url) fetches at once
    const tick = async (force = false) => {
      if (busy && !force) return
      busy = true
      try {
        const u = force ? url + (url.includes("?") ? "&" : "?") + "fresh=1" : url
        const res = await fetch(u, { cache: "no-store", signal: AbortSignal.timeout(timeoutMs) })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const json = (await res.json()) as T
        if (!alive) return
        setData(json)
        fails.current = 0
        setOk(true)
        setError("")
      } catch (err) {
        if (!alive) return
        fails.current += 1
        if (fails.current >= 3) {
          setOk(false)
          setError(err instanceof Error && err.name === "TimeoutError" ? "Bot not answering" : "Can't reach the bot")
        }
      } finally {
        busy = false
      }
    }
    tickRef.current = tick
    tick()
    const id = setInterval(() => tick(), everyMs)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [url, everyMs, timeoutMs])

  /** Fetch now, skipping any server-side cache (the Sync button). */
  const refresh = () => tickRef.current(true)
  return { data, ok, error, refresh }
}

export type Headline = {
  ts: number; source: string; author: string; followers: number | null; text: string; url: string
  ticker: string; name: string; words: string[]
}

export type Narratives = {
  now: number
  themes: { word: string; launches: number; ran_2x: number; hit_pct: number; tweets: number; news: number; max_followers: number }[]
  tweets: Headline[]; ai: Headline[]; news: Headline[]
  counts: Record<string, number>
  clusters?: { key: string; og: string; symbol: string; og_ts: number; copies: number; last_copy_ts: number; og_x: number | null; og_mcap_sol: number | null }[]
}

export type TokenCard = {
  error?: string
  mint: string; symbol: string; name: string; pool: string; migrated: boolean
  features: Record<string, number | boolean | null>
  red_flags: string[]; green_flags: string[]
  creator: { wallet: string; banned: boolean; ban_reason: string; whale: string } | null
  buyers: { wallet: string; label: string; first_buy: number; whale: boolean; banned: boolean; sold: boolean }[]
  words: string[]; story: Headline[]; links: Record<string, string>
}

export type TapeItem = {
  ts: number; side: "buy" | "sell" | "missed"; key: string; role: string; shared: boolean; mint: string; symbol: string
  sol?: number; price?: number; why?: string[]; x?: number | null; partial?: boolean
  pnl_sol?: number; pnl_pct?: number; hold_s?: number
}

export type TradesView = {
  now: number
  tape: TapeItem[]
  crowded: { mint: string; symbol: string; holders: { key: string; role: string; x: number | null; entry_ts: number }[] }[]
  stats: { buys: number; closed: number; won: number; pnl_sol: number }
}
