import { useMemo, useState } from "react"
import { AlertTriangle, ArrowDownRight, ArrowUpRight, Users } from "lucide-react"

import { Empty, RoleBadge, Section, Stat, StrategyName } from "@/components/app/bits"
import { CoinLink } from "@/components/app/narratives"
import { Badge } from "@/components/ui/badge"
import type { TapeItem, TradesView } from "@/lib/api"
import { ago, duration, int, num, pct, signedSol, tone } from "@/lib/format"
import { cn } from "@/lib/utils"

type Who = "all" | "champion" | "swarm" | "trial"
const WHO: [Who, string][] = [["all", "Everyone"], ["champion", "Champions (real wallet)"], ["swarm", "Swarm"], ["trial", "Trials"]]
const SIDE: [string, string][] = [["all", "Buys & sells"], ["buy", "Buys"], ["sell", "Sells"], ["missed", "Missed"]]

const roleOf = (r: string): Who => (r === "champion" ? "champion" : r === "swarm" ? "swarm" : "trial")

export function TradesPage({ data }: { data: TradesView | null }) {
  const [who, setWho] = useState<Who>("all")
  const [side, setSide] = useState("all")
  const [family, setFamily] = useState("")
  const [more, setMore] = useState(false)
  const tape = data?.tape ?? []
  const families = useMemo(() => [...new Set(tape.map(t => t.key.split("@")[0]))].sort(), [tape])
  const rows = tape.filter(t => (who === "all" || roleOf(t.role) === who) && (side === "all" || t.side === side)
    && (!family || t.key.startsWith(family + "@")))
  const shown = more ? rows : rows.slice(0, 40)
  const s = data?.stats
  const now = data?.now ?? Date.now() / 1000
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
        <Stat label="Buys" value={s ? int(s.buys) : "—"} sub="in the latest 200 fills" />
        <Stat label="Closed" value={s ? int(s.closed) : "—"} sub="sells with a result, this session" />
        <Stat label="Won" value={s && s.closed ? pct((s.won / s.closed) * 100, 0) : "—"} sub={s ? `${s.won} of ${s.closed} closed in profit` : ""} />
        <Stat label="Realised PnL" value={s ? `${signedSol(s.pnl_sol)} SOL` : "—"} tone={tone(s?.pnl_sol)} sub="all wallets, paper" />
      </div>
      <Section title={<span className="flex items-center gap-2"><span className="relative flex size-2"><span className="absolute inline-flex size-full animate-ping rounded-full bg-positive opacity-60" /><span className="relative inline-flex size-2 rounded-full bg-positive" /></span>Live trades</span>}
        description={s ? `Every agent's buys and sells this session · ${s.buys} buys · ${s.closed} closed, ${s.won} won · ${signedSol(s.pnl_sol)} SOL. Swarm and trial trades are paper experiments on their own wallets; only champions trade the real wallet.` : "Every agent's buys and sells this session."}>
        <div className="mb-3 flex flex-wrap items-center gap-1.5">
          {WHO.map(([k, label]) => <Chip key={k} on={who === k} onClick={() => setWho(k)}>{label}</Chip>)}
          <span className="mx-1 h-4 w-px bg-border" />
          {SIDE.map(([k, label]) => <Chip key={k} on={side === k} onClick={() => setSide(k)}>{label}</Chip>)}
          <select value={family} onChange={e => setFamily(e.target.value)}
            className="ml-auto h-7 rounded-md border bg-background px-2 text-xs">
            <option value="">all families</option>
            {families.map(f => <option key={f} value={f}>{f}</option>)}
          </select>
        </div>
        {!data ? <Empty>Loading…</Empty> : shown.length === 0 ? <Empty>No trades match yet. Trades show up here the moment they fill.</Empty> : (
          <ol className="divide-y">
            {shown.map(t => <TapeRow key={`${t.ts}-${t.key}-${t.side}-${t.mint}`} t={t} now={now} />)}
          </ol>
        )}
        {rows.length > 40 && (
          <button type="button" onClick={() => setMore(m => !m)} className="mt-2 w-full rounded-md border py-1.5 text-xs hover:bg-muted">
            {more ? "show fewer" : `show all ${rows.length}`}
          </button>
        )}
      </Section>
    </div>
  )
}

function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button type="button" onClick={onClick}
      className={cn("h-7 rounded-full border px-2.5 text-xs transition-colors", on ? "border-foreground/30 bg-foreground text-background" : "hover:bg-muted")}>
      {children}
    </button>
  )
}

/** One fill. `compact` drops the entry reasons (the overview's short list). */
export function TapeRow({ t, now, compact }: { t: TapeItem; now: number; compact?: boolean }) {
  const closed = t.side === "sell" && t.pnl_sol != null
  const win = (t.pnl_sol ?? 0) > 0
  const Icon = t.side === "buy" ? ArrowUpRight : t.side === "sell" ? ArrowDownRight : AlertTriangle
  const iconTone = t.side === "buy" ? "bg-chart-2/15 text-chart-2" : t.side === "missed" ? "bg-warning/15 text-warning"
    : closed ? (win ? "bg-positive/15 text-positive" : "bg-negative/15 text-negative") : "bg-positive/15 text-positive"
  return (
    <li className="flex gap-3 py-2 animate-in fade-in slide-in-from-top-1">
      <span className={cn("mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full", iconTone)}><Icon className="size-4" /></span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-sm">
          <span className="font-medium">{t.side === "buy" ? "bought" : t.side === "missed" ? "missed" : t.partial ? "took profit on" : "sold"}</span>
          <CoinLink mint={t.mint}>{t.symbol || t.mint.slice(0, 6)}</CoinLink>
          {t.sol != null && t.sol > 0 && <span className="num text-muted-foreground">{num(t.sol, 3)} SOL</span>}
          {closed && (
            <span className={cn("num font-medium", win ? "text-positive" : "text-negative")}>
              {signedSol(t.pnl_sol)} ({pct(t.pnl_pct, 0, true)})
            </span>
          )}
          {t.side === "sell" && t.x != null && <span className="num text-xs text-muted-foreground">{num(t.x, 2)}x</span>}
          <span className="ml-auto shrink-0 text-xs text-muted-foreground">{ago(t.ts, now)}</span>
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-xs">
          <StrategyName k={t.key} className="text-xs" />
          <RoleBadge role={t.role} />
          {!compact && t.side === "buy" && (t.why ?? []).map(w => <Badge key={w} variant="outline" className="font-mono text-[10px] font-normal">{w}</Badge>)}
          {!compact && t.side !== "buy" && (t.why ?? []).map(w => <Badge key={w} variant="secondary" className="text-[10px]">{w.replace(/_/g, " ")}</Badge>)}
          {closed && t.hold_s != null && <span className="text-muted-foreground">held {duration(t.hold_s)}</span>}
        </div>
      </div>
    </li>
  )
}

export function Crowded({ data }: { data: TradesView | null }) {
  const c = data?.crowded ?? []
  if (!c.length) return null
  return (
    <Section title={<span className="flex items-center gap-2"><Users className="size-4" />Crowded coins</span>}
      description="Coins several agents hold right now. When different strategies agree independently, that is a signal worth watching.">
      <div className="-mx-1 flex gap-3 overflow-x-auto px-1 pb-1">
        {c.map(coin => {
          const champ = coin.holders.some(h => h.role === "champion")
          const xs = coin.holders.map(h => h.x).filter((x): x is number => x != null)
          const avg = xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null
          return (
            <div key={coin.mint} className={cn("w-56 shrink-0 rounded-lg border p-3", champ && "border-chart-3/50")}>
              <div className="flex items-center justify-between gap-2">
                <CoinLink mint={coin.mint} className="truncate">{coin.symbol || coin.mint.slice(0, 6)}</CoinLink>
                <Badge className="bg-chart-1/15 text-chart-1">{coin.holders.length} agents</Badge>
              </div>
              <div className={cn("num mt-1 text-lg font-semibold", avg == null ? "" : avg >= 1 ? "text-positive" : "text-negative")}>
                {avg == null ? "—" : `${num(avg, 2)}x`} <span className="text-xs font-normal text-muted-foreground">avg since their buys</span>
              </div>
              <ul className="mt-2 space-y-0.5 text-xs">
                {coin.holders.slice(0, 5).map(h => (
                  <li key={h.key} className="flex items-center justify-between gap-2">
                    <StrategyName k={h.key} className="text-xs" />
                    <span className={cn("num", h.x == null ? "text-muted-foreground" : h.x >= 1 ? "text-positive" : "text-negative")}>{h.x == null ? "—" : `${num(h.x, 2)}x`}</span>
                  </li>
                ))}
                {coin.holders.length > 5 && <li className="text-muted-foreground">+{coin.holders.length - 5} more</li>}
              </ul>
            </div>
          )
        })}
      </div>
    </Section>
  )
}
