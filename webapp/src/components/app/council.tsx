import { useState } from "react"
import { Check, X } from "lucide-react"

import { Empty, Section, ShowMore } from "@/components/app/bits"
import { CouncilChat } from "@/components/app/council-chat"
import { CursorUsageCard } from "@/components/app/cursor-usage"
import { CouncilChamber } from "@/components/app/visualizer"
import { Badge } from "@/components/ui/badge"
import type { CouncilIdea, Snapshot, Swarm } from "@/lib/api"
import { num, signedSol, tone } from "@/lib/format"
import { cn } from "@/lib/utils"

const STATUS: Record<CouncilIdea["status"], string> = {
  working: "bg-positive/15 text-positive",
  testing: "bg-chart-2/15 text-chart-2",
  failed: "bg-negative/15 text-negative",
  rejected: "bg-muted text-muted-foreground",
  unhelpful: "bg-muted text-muted-foreground/70",
}

const FILTERS: [string, string, (i: CouncilIdea) => boolean][] = [
  ["live", "Working & testing", i => i.status === "working" || i.status === "testing"],
  ["failed", "Failed live", i => i.status === "failed"],
  ["dropped", "Dropped", i => i.status === "rejected" || i.status === "unhelpful"],
  ["all", "All", () => true],
]

export function CouncilView({ swarm, snap }: { swarm: Swarm; snap: Snapshot }) {
  const [filter, setFilter] = useState("live")
  const c = swarm.council
  if (!c) return <Empty>Council data unavailable.</Empty>
  const ideas = c.ideas
  const tested = ideas.filter(i => i.approved)
  const shown = ideas.filter(FILTERS.find(f => f[0] === filter)![2])
  return (
    <div className="space-y-4">
      <CursorUsageCard />

      <CouncilChamber swarm={swarm} snap={snap} after={<CouncilChat />} />

      <Section title="Ideas" description={`${tested.length} approved and bred into swarm children · ${ideas.length - tested.length} dropped by the replay check or the vote. Approved ideas are judged by their carriers' live trades.`}>
          {ideas.length === 0 ? <Empty>No sittings yet. The council meets during each swarm generation, for strategies with at least 20 recent trades.</Empty> : (
            <>
              <div className="mb-3 flex flex-wrap gap-1.5">
                {FILTERS.map(([k, label, f]) => (
                  <button key={k} type="button" onClick={() => setFilter(k)}
                    className={cn("h-7 rounded-full border px-2.5 text-xs transition-colors", filter === k ? "border-foreground/30 bg-foreground text-background" : "hover:bg-muted")}>
                    {label} <span className="num opacity-70">{ideas.filter(f).length}</span>
                  </button>
                ))}
              </div>
              {shown.length === 0 ? <Empty>No ideas here.</Empty> : (
                <ShowMore key={filter} items={shown} n={8}>{list => (
                  <div className="space-y-2">{list.map(i => <IdeaRow key={i.idea_id} i={i} />)}</div>
                )}</ShowMore>
              )}
            </>
          )}
      </Section>
    </div>
  )
}

function IdeaRow({ i }: { i: CouncilIdea }) {
  const verb = i.kind === "require" ? "buy only when" : "don't buy when"
  const votes = Object.entries(i.votes)
  const s = i.in_sample
  return (
    <div className={cn("min-w-0 rounded-lg border p-3", !i.approved && "opacity-70")}>
      <div className="flex flex-wrap items-center gap-2">
        <Badge className={STATUS[i.status]}>{i.status}</Badge>
        <span className="font-mono text-sm [overflow-wrap:anywhere]">{verb} {i.feature} {i.op} {Number(i.v.toPrecision(4))}</span>
        <span className="text-xs text-muted-foreground">· {i.strategy_id} · by {i.proposed_by} · gen {i.gen}</span>
      </div>
      {i.reason && <p className="mt-1.5 text-sm text-muted-foreground">“{i.reason}”</p>}
      <div className="mt-2 grid gap-2 text-xs md:grid-cols-3">
        <div className="rounded-md bg-muted/40 p-2">
          <div className="text-muted-foreground">Replay check</div>
          <div className="num mt-0.5">{s.saved_sol != null ? <span className={tone(s.saved_sol)}>{s.saved_sol >= 0 ? "saves" : "costs"} {Math.abs(s.saved_sol).toFixed(3)} SOL</span> : "—"}
            {s.blocked != null && <span className="text-muted-foreground"> · blocks {s.blocked}/{s.n} ({s.winners_lost ?? 0} winners)</span>}</div>
        </div>
        <div className="rounded-md bg-muted/40 p-2">
          <div className="text-muted-foreground">Vote {i.score != null && <span className="num">· score {num(i.score, 2)}</span>}</div>
          <div className="mt-1 flex flex-wrap gap-1">
            {votes.length === 0 ? <span className="text-muted-foreground">not voted</span> : votes.map(([name, v]) => (
              <span key={name} title={v.reason} className={cn("inline-flex items-center gap-1 rounded px-1.5 py-0.5", v.approve ? "bg-positive/15 text-positive" : "bg-negative/15 text-negative")}>
                {v.approve ? <Check className="size-3" /> : <X className="size-3" />}{name} {Math.round(v.confidence * 100)}%
              </span>
            ))}
          </div>
        </div>
        <div className="rounded-md bg-muted/40 p-2">
          <div className="text-muted-foreground">Live test</div>
          <div className="num mt-0.5">{i.approved
            ? <>{i.carriers} carriers · {i.live_trades} trades · <span className={tone(i.live_pnl_sol)}>{signedSol(i.live_pnl_sol)} SOL</span>{i.deaths ? <span className="text-negative"> · {i.deaths} died</span> : null}</>
            : <span className="text-muted-foreground">not tested</span>}</div>
        </div>
      </div>
      {i.status_reason && <p className="mt-2 text-xs text-muted-foreground">{i.status_reason}</p>}
    </div>
  )
}
