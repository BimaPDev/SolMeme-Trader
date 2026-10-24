import { Crosshair, Eye, Scale, Search, ShieldCheck, type LucideIcon } from "lucide-react"
import type { ReactNode } from "react"

import { Empty } from "@/components/app/bits"
import { Badge } from "@/components/ui/badge"
import { usePoll } from "@/lib/api"
import { ago } from "@/lib/format"
import { cn } from "@/lib/utils"

type Team = {
  error?: string
  research?: { sources: { name: string; status: string; events: number; quiet_s: number | null }[]; early_lane: boolean; whales_watched: number }
  sniper?: { buys: number; missed: number; blocked: number; miss_reasons: Record<string, number>; slippage_limit_pct: number; reviews: Record<string, number> }
  trader?: { champions: string[]; open_live: number; sells: number; loss_limit_pct: number; rules: string[] }
  council?: { judges: string[]; backups: string[]; picks: Record<string, number>; reviews: Record<string, number> }
  overseer?: { paused: Record<string, string>; log: { ts: number; guard: string; reason: string; symbol?: string }[]
    guards: { max_data_age_s: number; day_loss_pct: number; max_exposure_pct: number; council_loss_streak: number } } | null
}

function Card({ icon: Icon, name, role, tone, children }: { icon: LucideIcon; name: string; role: string; tone: string; children: ReactNode }) {
  return (
    <div className="rounded-xl border bg-card p-4">
      <div className="flex items-center gap-3">
        <div className={cn("flex size-10 items-center justify-center rounded-full", tone)}><Icon className="size-5" /></div>
        <div><div className="font-semibold">{name}</div><div className="text-xs text-muted-foreground">{role}</div></div>
      </div>
      <div className="mt-3 space-y-1.5 text-sm">{children}</div>
    </div>
  )
}
const Row = ({ k, v }: { k: string; v: ReactNode }) => <div className="flex justify-between gap-3"><span className="text-muted-foreground">{k}</span><span className="num text-right">{v}</span></div>
const counts = (c?: Record<string, number>) => (c && Object.keys(c).length ? Object.entries(c).map(([k, v]) => `${v} ${k}`).join(" · ") : "none yet")

/** The team: who does what, and what each one is doing right now (last 24 h). */
export function TeamPage() {
  const { data: t } = usePoll<Team>("api/team", 5000)
  if (!t) return <Empty>Loading the team…</Empty>
  if (t.error) return <Empty>Team view unavailable: {t.error}</Empty>
  const ov = t.overseer
  const paused = Object.entries(ov?.paused ?? {})
  return (
    <div className="space-y-4">
      <div className="rounded-xl border bg-muted/30 px-4 py-2.5 text-sm text-muted-foreground">
        ResearchBot finds coins, SniperBot checks each entry at the real price, TraderBot trades inside your rules, the council decides, and the Overseer can stop new live buys when something is wrong. Paper keeps learning either way. Last 24 hours.
      </div>
      <div className={cn("rounded-xl border px-4 py-3", paused.length ? "border-negative/50 bg-negative/10" : "border-positive/40 bg-positive/5")}>
        <div className="flex items-center gap-2 font-semibold"><Eye className="size-4" /> Overseer: {paused.length ? "holding new live buys" : "all clear"}</div>
        {paused.map(([g, why]) => <div key={g} className="mt-1 text-sm text-negative">{g}: {why}</div>)}
        {ov && <div className="mt-1 text-xs text-muted-foreground">Stops new live buys if data goes quiet {ov.guards.max_data_age_s} s+, the wallet loses {ov.guards.day_loss_pct}% in 24 h, more than {ov.guards.max_exposure_pct}% is in positions, or the council's last {ov.guards.council_loss_streak} live picks all lost.</div>}
      </div>
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <Card icon={Search} name="ResearchBot" role="Hunts the alpha" tone="bg-chart-2/15 text-chart-2">
          {(t.research?.sources ?? []).map(s => (
            <Row key={s.name} k={s.name} v={<span className={s.quiet_s != null && s.quiet_s > 120 ? "text-negative" : ""}>{s.events.toLocaleString()} · {s.quiet_s != null ? `${s.quiet_s}s ago` : s.status}</span>} />
          ))}
          <Row k="whales watched" v={t.research?.whales_watched ?? "—"} />
          <Row k="early-runner lane" v={t.research?.early_lane ? "on" : "off"} />
        </Card>
        <Card icon={Crosshair} name="SniperBot" role="Watches your entries" tone="bg-negative/15 text-negative">
          <Row k="live buys" v={t.sniper?.buys ?? 0} />
          <Row k="refused at the real price" v={t.sniper?.missed ?? 0} />
          {Object.entries(t.sniper?.miss_reasons ?? {}).map(([k, v]) => <Row key={k} k={`· ${k}`} v={v} />)}
          <Row k="slippage limit" v={`${t.sniper?.slippage_limit_pct ?? "—"}%`} />
          <Row k="stopped by the Overseer" v={t.sniper?.blocked ?? 0} />
        </Card>
        <Card icon={ShieldCheck} name="TraderBot" role="Trades inside your rules" tone="bg-chart-1/15 text-chart-1">
          <div className="flex flex-wrap gap-1">{(t.trader?.champions ?? []).map(c => <Badge key={c} className="bg-chart-1/15 text-chart-1">{c.split("@")[0]}</Badge>)}</div>
          <Row k="live positions open" v={t.trader?.open_live ?? 0} />
          <Row k="live sells" v={t.trader?.sells ?? 0} />
          <Row k="kill switch" v={`-${t.trader?.loss_limit_pct ?? 50}%`} />
          <div className="text-xs text-muted-foreground">Your rules: {(t.trader?.rules ?? []).join(", ")}</div>
        </Card>
        <Card icon={Scale} name="Council" role="Decides" tone="bg-chart-3/15 text-chart-3">
          <div className="text-xs">{(t.council?.judges ?? []).join(", ")}{t.council?.backups?.length ? <span className="text-muted-foreground"> · backup: {t.council.backups.join(", ")}</span> : null}</div>
          <Row k="auto-picks" v={counts(t.council?.picks)} />
          <Row k="live-buy reviews" v={counts(t.council?.reviews)} />
        </Card>
      </div>
      <div className="rounded-xl border bg-card p-4">
        <div className="font-semibold">Overseer log</div>
        {!ov?.log?.length ? <div className="mt-2 text-sm text-muted-foreground">Nothing stopped yet.</div> : (
          <ul className="mt-2 divide-y text-sm">
            {ov.log.map(l => <li key={l.ts + l.guard} className="flex flex-wrap gap-2 py-1"><Badge className="bg-negative/15 text-negative">{l.guard}</Badge>
              {l.symbol && <span className="font-medium">{l.symbol}</span>}<span className="text-muted-foreground">{l.reason}</span><span className="num ml-auto text-xs text-muted-foreground">{ago(l.ts)}</span></li>)}
          </ul>
        )}
      </div>
    </div>
  )
}
