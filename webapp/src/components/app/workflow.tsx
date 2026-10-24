import { Loader2 } from "lucide-react"
import { useEffect, useState } from "react"

import { Empty, Section } from "@/components/app/bits"
import { CoinLink } from "@/components/app/narratives"
import { Badge } from "@/components/ui/badge"
import { usePoll, type Snapshot } from "@/lib/api"
import { ago, num, usd } from "@/lib/format"
import { cn } from "@/lib/utils"

type Brief = {
  mint: string; symbol: string; age_s: number; mcap_usd: number | null; ts: number; match: "bought" | "nearly"; by: string[]; champion: boolean
  wallets: { creator: string | null; creator_banned: boolean; dev_prior_launches: number | null; dev_prior_rugs: number | null; dev_sold_pct: number | null
    banned_in: number | null; whales_in: number | null; whales_selling: number | null; kols_in: number | null
    dev_chain_launches: number | null; dev_chain_grad_pct: number | null; dev_wallet_age_h: number | null }
  holders: { count: number | null; top10_pct: number | null; sniper_count: number | null; bundler_rate: number | null
    linked_wallets: number | null; linked_pct: number | null; fresh_pct: number | null }
  liquidity: { pool_usd: number | null; round_trip_pct: number | null; migrated: boolean }
  flag: "green" | "yellow" | "red"; red: string[]; yellow: string[]; green: string[]
}
const FLAG = { green: "bg-positive/15 text-positive", yellow: "bg-chart-2/15 text-chart-2", red: "bg-negative/15 text-negative" }
const dash = (v: unknown) => (v == null ? "—" : String(v))

/** ResearchBot: your eyes on the trenches. */
export function ResearchPage() {
  const { data } = usePoll<Brief[] | { error: string }>("api/research", 6000, 15000)
  const [only, setOnly] = useState<"all" | "champion" | "green">("all")
  if (!data) return <Empty>Loading research…</Empty>
  if (!Array.isArray(data)) return <Empty>Research unavailable: {data.error}</Empty>
  const rows = data.filter(b => only === "all" || (only === "champion" ? b.champion : b.flag === "green"))
  return (
    <Section title="ResearchBot · your eyes on the trenches" description="Every coin a strategy bought or nearly bought in the last hour, already checked: wallets, holders and liquidity, so you don't open every token. No AI calls.">
      <div className="mb-3 flex gap-2 text-xs">
        {(["all", "champion", "green"] as const).map(k => (
          <button key={k} type="button" onClick={() => setOnly(k)} className={cn("rounded-full border px-2.5 py-1", only === k && "bg-foreground text-background")}>
            {k === "all" ? "all matches" : k === "champion" ? "champion matches" : "green only"}</button>
        ))}
      </div>
      {rows.length === 0 ? <Empty>No strategy matches in the last hour.</Empty> : (
        <ul className="space-y-2">
          {rows.map(b => (
            <li key={b.mint} className="rounded-lg border p-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <CoinLink mint={b.mint}>{b.symbol}</CoinLink>
                <Badge className={FLAG[b.flag]}>{b.flag}</Badge>
                <Badge className="bg-muted text-muted-foreground">{b.match === "bought" ? "bought by" : "nearly bought by"} {b.by.join(", ")}</Badge>
                {b.mcap_usd != null && <span className="num text-xs text-muted-foreground">mcap {usd(b.mcap_usd)} · {Math.round(b.age_s / 60)} min old</span>}
                <span className="num ml-auto text-xs text-muted-foreground">{ago(b.ts)}</span>
              </div>
              <div className="num mt-2 grid gap-2 text-xs sm:grid-cols-3">
                <div className="rounded-md bg-muted/40 p-2"><div className="text-muted-foreground">Wallets</div>
                  <div>creator {dash(b.wallets.creator)}{b.wallets.creator_banned ? " (banned)" : ""} · earlier coins {dash(b.wallets.dev_prior_launches)}, rugged {dash(b.wallets.dev_prior_rugs)}</div>
                  <div>whales in {dash(b.wallets.whales_in)} (selling {dash(b.wallets.whales_selling)}) · KOLs {dash(b.wallets.kols_in)} · banned {dash(b.wallets.banned_in)}</div>
                  <div>on-chain: launched {dash(b.wallets.dev_chain_launches)} coins{b.wallets.dev_chain_grad_pct != null ? `, ${num(b.wallets.dev_chain_grad_pct, 0)}% graduated` : ""}
                    {b.wallets.dev_wallet_age_h != null ? ` · wallet ${num(b.wallets.dev_wallet_age_h, 0)} h old` : ""}</div></div>
                <div className="rounded-md bg-muted/40 p-2"><div className="text-muted-foreground">Holders</div>
                  <div>{dash(b.holders.count)} holders · top 10 {b.holders.top10_pct != null ? `${num(b.holders.top10_pct, 0)}%` : "—"}</div>
                  <div>snipers {dash(b.holders.sniper_count)} · bundled {b.holders.bundler_rate != null ? `${num(100 * b.holders.bundler_rate, 0)}%` : "—"}</div>
                  <div>same funder {dash(b.holders.linked_wallets)}{b.holders.linked_pct != null ? ` (${num(b.holders.linked_pct, 1)}%)` : ""} · fresh wallets {b.holders.fresh_pct != null ? `${num(b.holders.fresh_pct, 0)}%` : "—"}</div></div>
                <div className="rounded-md bg-muted/40 p-2"><div className="text-muted-foreground">Liquidity</div>
                  <div>pool {b.liquidity.pool_usd != null ? usd(b.liquidity.pool_usd) : "—"} · {b.liquidity.migrated ? "migrated" : "on the curve"}</div>
                  <div>buy + sell back now costs {b.liquidity.round_trip_pct != null ? `${num(b.liquidity.round_trip_pct, 1)}%` : "—"}</div></div>
              </div>
              {(b.red.length > 0 || b.yellow.length > 0 || b.green.length > 0) && (
                <div className="mt-1.5 flex flex-wrap gap-x-3 text-xs">
                  {b.red.map(x => <span key={x} className="text-negative">✕ {x}</span>)}
                  {b.yellow.map(x => <span key={x} className="text-chart-2">! {x}</span>)}
                  {b.green.map(x => <span key={x} className="text-positive">✓ {x}</span>)}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </Section>
  )
}

type Setup = { mint: string; symbol: string; strategy: string; since: number; status: "waiting" | "ready"; mcap_usd: number | null
  waiting: { rule: string; feature?: string; op?: string; need?: unknown; now?: unknown }[]
  ticket: { size_sol: number; size_usd: number | null; max_slippage_pct: number; take_profit: string[]; stop_pct: number | null; live_allowed: boolean } }
const fmtv = (v: unknown): string => (typeof v === "number" ? num(v, Math.abs(v) < 10 ? 2 : 0) : Array.isArray(v) ? v.map(fmtv).join("–") : v == null ? "—" : String(v))

/** SniperBot: watches entries, waits for your conditions, gets the trade ready. */
export function SetupsPage() {
  const { data } = usePoll<Setup[] | { error: string }>("api/setups", 5000, 15000)
  if (!data) return <Empty>Loading setups…</Empty>
  if (!Array.isArray(data)) return <Empty>Setups unavailable: {data.error}</Empty>
  return (
    <Section title="SniperBot · setups" description="Coins a champion is watching, one or two conditions away from its entry. It shows what it's waiting for, and the trade ticket that fires the moment the last condition hits.">
      {data.length === 0 ? <Empty>No champion setups in the last 30 minutes.</Empty> : (
        <ul className="space-y-2">
          {data.map(s => (
            <li key={s.strategy + s.mint} className="rounded-lg border p-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <CoinLink mint={s.mint}>{s.symbol}</CoinLink>
                <Badge className={s.status === "ready" ? "bg-positive/15 text-positive" : "bg-chart-2/15 text-chart-2"}>{s.status}</Badge>
                <Badge className="bg-muted text-muted-foreground">{s.strategy}</Badge>
                {s.mcap_usd != null && <span className="num text-xs text-muted-foreground">mcap {usd(s.mcap_usd)}</span>}
                <span className="num ml-auto text-xs text-muted-foreground">watching since {ago(s.since)}</span>
              </div>
              <div className="mt-1.5 text-xs">Waiting for: {s.waiting.map((w, i) => (
                <span key={i} className="mr-3">{w.feature ? <><span className="font-medium">{w.feature}</span> {w.op} {fmtv(w.need)} <span className="text-muted-foreground">(now {fmtv(w.now)})</span></> : w.rule}</span>
              ))}</div>
              <div className="mt-1 text-xs text-muted-foreground">Ticket: {s.ticket.size_usd != null ? usd(s.ticket.size_usd) : `${s.ticket.size_sol} SOL`} · max slippage {s.ticket.max_slippage_pct}% · {s.ticket.take_profit.join(", ")}{s.ticket.stop_pct != null ? ` · stop −${s.ticket.stop_pct}%` : ""}
                {!s.ticket.live_allowed && <span className="text-negative"> · live not allowed for this strategy</span>}</div>
            </li>
          ))}
        </ul>
      )}
    </Section>
  )
}

type Perm = { strategies: Record<string, boolean>; max_bet_usd: number; max_open: number; max_trades_per_day: number; council_picks: boolean }

/** TraderBot: execution with the wallet, size and permissions you set. */
export function TraderPage({ snap }: { snap: Snapshot }) {
  const [p, setP] = useState<Perm | null>(null)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState("")
  useEffect(() => { fetch("api/permissions").then(r => r.json()).then(setP).catch(() => setMsg("Can't reach the bot")) }, [])
  const champs = [...new Set(snap.strategies.filter(s => s.role === "champion").map(s => s.key.split("@")[0]))]
  async function save(changes: Partial<Perm>) {
    setSaving(true); setMsg("")
    try {
      const r = await fetch("api/permissions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes) })
      setP(await r.json()); setMsg("Saved. Live uses it from the next buy.")
    } catch { setMsg("Can't reach the bot") } finally { setSaving(false) }
  }
  if (!p) return <Empty>{msg || "Loading permissions…"}</Empty>
  const numField = (k: "max_bet_usd" | "max_open" | "max_trades_per_day", label: string, hint: string) => (
    <label className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
      <span>{label}<span className="block text-xs text-muted-foreground">{hint}</span></span>
      <input type="number" defaultValue={p[k]} min={0} step={k === "max_bet_usd" ? 1 : 1} onBlur={e => save({ [k]: Number(e.target.value) } as Partial<Perm>)}
             className="h-9 w-28 rounded-md border bg-background px-2 text-right font-mono" />
    </label>
  )
  return (
    <div className="space-y-4">
      <Section title="TraderBot · permissions" description="Execution with the wallet, size and permissions you set. It uses the work already done (the research brief, the setup's ticket, the council's call), so each trade isn't rebuilt. These apply to the live wallet; paper keeps learning with everything.">
        <div className="divide-y">
          {numField("max_bet_usd", "Max bet per trade ($)", "the strategy's own size, capped here")}
          {numField("max_open", "Max open positions", "no new buys while this many are open")}
          {numField("max_trades_per_day", "Max live trades per day", "")}
          <label className="flex items-center justify-between gap-2 py-2 text-sm">
            <span>Council picks may buy<span className="block text-xs text-muted-foreground">whale clusters and the early-runner lane, when the council votes Buy</span></span>
            <input type="checkbox" checked={p.council_picks} onChange={e => save({ council_picks: e.target.checked })} className="size-5" />
          </label>
        </div>
      </Section>
      <Section title="Strategies allowed to trade the live wallet" description="Switch a champion off and live stops copying it; it keeps trading on paper.">
        <ul className="divide-y">
          {champs.map(sid => (
            <li key={sid} className="flex items-center justify-between py-2 text-sm">
              <span className="font-medium">{sid}</span>
              <input type="checkbox" checked={p.strategies[sid] !== false} onChange={e => save({ strategies: { [sid]: e.target.checked } } as Partial<Perm>)} className="size-5" />
            </li>
          ))}
        </ul>
        <div className="mt-2 text-xs text-muted-foreground">{saving ? <Loader2 className="inline size-3 animate-spin" /> : null} {msg}</div>
      </Section>
    </div>
  )
}
