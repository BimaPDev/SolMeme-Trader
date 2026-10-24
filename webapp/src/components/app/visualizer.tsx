import { useEffect, useMemo, useRef, useState, type ComponentType, type ReactNode } from "react"
import {
  Archive, Ban, BookOpen, Brain, CircleCheck, Crown, Dna, Fish, Funnel, Gavel, Lightbulb, Scale, ShieldAlert, Skull,
  Sparkles, TrendingUp, Trophy, Zap,
} from "lucide-react"

import { Empty, Section } from "@/components/app/bits"
import { Badge } from "@/components/ui/badge"
import type { Agent, CouncilIdea, CouncilMember, Snapshot, Swarm, TapeItem } from "@/lib/api"
import { ago, duration, num, pct, signedSol } from "@/lib/format"
import { cn } from "@/lib/utils"

type Icon = ComponentType<{ className?: string }>

// ── 1. the generation pipeline ─────────────────────────────────────────────
const STEPS: { phase: string; label: string; hint: string; icon: Icon }[] = [
  { phase: "judging", label: "Judge", hint: "kill losers, credit what worked", icon: Gavel },
  { phase: "learning", label: "Learn", hint: "replay the last 24h into lessons", icon: BookOpen },
  { phase: "learning from winners", label: "Study winners", hint: "what the best agents have in common", icon: Trophy },
  { phase: "breeding", label: "Breed", hint: "mutate and cross the survivors", icon: Dna },
  { phase: "screening", label: "Screen", hint: "replay every child before it goes live", icon: Funnel },
  { phase: "stress test", label: "Stress test", hint: "worse fills, fees, delays", icon: Zap },
  { phase: "ban scan", label: "Ban scan", hint: "pump-and-dump wallets", icon: ShieldAlert },
  { phase: "whale scan", label: "Whale scan", hint: "re-score smart wallets", icon: Fish },
  { phase: "council", label: "Council", hint: "LLM judges propose and vote on rules", icon: Scale },
  { phase: "done", label: "Done", hint: "children are trading live", icon: CircleCheck },
]

export function GenerationPipeline({ swarm, snap }: { swarm: Swarm; snap: Snapshot }) {
  const clock = snap.swarm_clock
  const now = snap.now
  const running = !!clock?.running
  const cur = running ? STEPS.findIndex(s => s.phase === clock?.phase) : -1
  const last = swarm.generations.at(-1)
  const every = (clock?.every_min ?? swarm.config.every_min) * 60
  const nextIn = clock?.next_ts ? clock.next_ts - now : null
  const waited = nextIn != null ? Math.min(1, Math.max(0, 1 - nextIn / every)) : 0
  const rail = useRef<HTMLDivElement>(null)
  useEffect(() => {                 // on a phone the rail scrolls: keep the current step in view
    const el = rail.current
    const step = el?.querySelector<HTMLElement>("[data-now]")
    if (el && step) el.scrollTo({ left: step.offsetLeft - el.clientWidth / 2 + step.clientWidth / 2, behavior: "smooth" })
  }, [cur])
  return (
    <Section
      title={<span className="flex items-center gap-2">
        {running ? <span className="relative flex size-2.5"><span className="absolute inline-flex size-full animate-ping rounded-full bg-chart-1 opacity-75" /><span className="relative inline-flex size-2.5 rounded-full bg-chart-1" /></span>
          : <span className="size-2.5 rounded-full bg-muted-foreground/40" />}
        {running ? `Generation ${swarm.generation + 1} evolving` : `Generation ${swarm.generation || "—"} is trading`}
      </span>}
      description={running
        ? `${clock?.started_ts ? duration(now - clock.started_ts) : "just"} in · ${STEPS[cur]?.hint ?? "starting"}`
        : nextIn != null ? `next generation in ${nextIn > 0 ? duration(nextIn) : "a moment"} · runs every ${swarm.config.every_min} min` : ""}>
      <div ref={rail} className="relative -mx-1 overflow-x-auto px-1 pb-1">
        <ol className="flex min-w-[640px] items-start">
          {STEPS.map((s, i) => {
            const state = !running ? "idle" : i < cur ? "done" : i === cur ? "now" : "todo"
            const I = s.icon
            return (
              <li key={s.phase} data-now={state === "now" ? "" : undefined} className="relative flex flex-1 flex-col items-center text-center">
                {i > 0 && <span className={cn("absolute left-0 right-1/2 top-4 h-0.5 -translate-y-1/2",
                  state === "done" || state === "now" ? "bg-chart-1" : "bg-border")} />}
                {i < STEPS.length - 1 && <span className={cn("absolute left-1/2 right-0 top-4 h-0.5 -translate-y-1/2",
                  state === "done" ? "bg-chart-1" : "bg-border")} />}
                <span className={cn("relative z-10 flex size-8 items-center justify-center rounded-full border-2 bg-background transition-colors",
                  state === "done" && "border-chart-1 bg-chart-1 text-background",
                  state === "now" && "border-chart-1 text-chart-1 shadow-[0_0_0_4px] shadow-chart-1/20",
                  (state === "todo" || state === "idle") && "border-border text-muted-foreground")}>
                  {state === "now" && <span className="absolute inset-0 animate-ping rounded-full border-2 border-chart-1 opacity-40" />}
                  <I className="size-4" />
                </span>
                <span className={cn("mt-1.5 text-[11px] leading-tight", state === "now" ? "font-semibold text-foreground" : "text-muted-foreground")}>{s.label}</span>
              </li>
            )
          })}
        </ol>
      </div>
      {!running && (
        <div className="mt-3 h-1 overflow-hidden rounded-full bg-muted">
          <div className="h-full rounded-full bg-chart-1/60 transition-[width] duration-1000" style={{ width: `${waited * 100}%` }} />
        </div>
      )}
      {last && (
        <div className="num mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span>last gen <span className="text-foreground">#{last.gen}</span> {last.ts ? ago(last.ts, now) : ""}</span>
          <span><Sparkles className="inline size-3 text-chart-1" /> {last.born} born</span>
          <span><Skull className="inline size-3 text-negative" /> {last.killed} killed</span>
          <span><Funnel className="inline size-3" /> {last.screened_out} failed screening</span>
          <span>{last.alive} alive</span>
          <span>best fitness <span className="text-foreground">{num(last.best, 1, true)}</span></span>
        </div>
      )}
    </Section>
  )
}

// ── 2. the swarm map: every agent by generation and family, linked to its parent ─────
const GEN_W = 30
const ROW_H = 18
const PAD_L = 112
const PAD_T = 22

type Node = { a: Agent; x: number; y: number; r: number }

const FLASH_S = 90

export function SwarmMap({ swarm, onPick, tape, now }: { swarm: Swarm; onPick: (a: Agent) => void; tape?: TapeItem[]; now?: number }) {
  const [hover, setHover] = useState<Node | null>(null)
  const [showDead, setShowDead] = useState(true)
  const lastGen = swarm.generation
  const layout = useMemo(() => {
    const agents = swarm.agents.filter(a => showDead || a.status !== "dead")
    const species = [...new Set(agents.map(a => a.species))]
      .sort((x, y) => agents.filter(a => a.species === y && a.status === "swarm").length - agents.filter(a => a.species === x && a.status === "swarm").length)
    const minGen = Math.min(...agents.map(a => a.gen), lastGen)
    const maxGen = Math.max(...agents.map(a => a.gen), lastGen)
    const lanes: { id: string; top: number; h: number }[] = []
    const nodes: Node[] = []
    let top = PAD_T
    for (const sp of species) {
      const mine = agents.filter(a => a.species === sp)
      const byGen = new Map<number, Agent[]>()
      for (const a of mine) byGen.set(a.gen, [...(byGen.get(a.gen) ?? []), a])
      const depth = Math.max(1, ...[...byGen.values()].map(v => v.length))
      for (const [g, list] of byGen) {
        list.sort((p, q) => (q.status === "swarm" ? 1 : 0) - (p.status === "swarm" ? 1 : 0) || q.equity - p.equity)
        list.forEach((a, i) => nodes.push({
          a, x: PAD_L + (g - minGen) * GEN_W + GEN_W / 2, y: top + ROW_H / 2 + i * ROW_H,
          r: Math.max(3.5, Math.min(9, 3 + Math.sqrt(a.live_n))),
        }))
      }
      lanes.push({ id: sp, top, h: depth * ROW_H + 6 })
      top += depth * ROW_H + 6
    }
    const byKey = new Map(nodes.map(n => [n.a.key, n]))
    const edges = nodes.flatMap(n => {
      const p = n.a.parent ? byKey.get(`${n.a.species}@${n.a.parent}`) : undefined
      const m = n.a.mate ? nodes.find(o => o.a.key.endsWith(`@${n.a.mate}`) || o.a.key === n.a.mate) : undefined
      return [p && { from: p, to: n, mate: false }, m && { from: m, to: n, mate: true }].filter(Boolean) as { from: Node; to: Node; mate: boolean }[]
    })
    return { nodes, edges, lanes, width: PAD_L + (maxGen - minGen + 1) * GEN_W + 16, height: top + 8, minGen, maxGen, byKey }
  }, [swarm.agents, lastGen, showDead])

  const lineage = useMemo(() => {
    const keys = new Set<string>()
    let n = hover
    while (n && !keys.has(n.a.key)) {
      keys.add(n.a.key)
      n = n.a.parent ? layout.byKey.get(`${n.a.species}@${n.a.parent}`) ?? null : null
    }
    return keys
  }, [hover, layout.byKey])

  const flash = useMemo(() => {        // agents that just bought or sold light up on the map
    const out = new Map<string, string>()
    for (const t of tape ?? []) {
      if (now == null || now - t.ts > FLASH_S || out.has(t.key) || t.side === "missed") continue
      out.set(t.key, t.side === "buy" ? "var(--chart-2)" : (t.pnl_sol ?? 1) > 0 ? "var(--positive)" : "var(--negative)")
    }
    return out
  }, [tape, now])

  if (!layout.nodes.length) return <Empty>No agents yet.</Empty>
  const fill = (a: Agent) => {
    if (a.status === "dead") return "var(--muted-foreground)"
    if (a.status === "champion") return "var(--chart-3)"
    return a.equity >= 1 ? "var(--positive)" : "var(--negative)"
  }
  const strength = (a: Agent) => a.status === "dead" ? 0.25 : Math.min(1, 0.45 + Math.abs(Math.log(Math.max(a.equity, 0.01))) / 2)
  const gens = Array.from({ length: layout.maxGen - layout.minGen + 1 }, (_, i) => layout.minGen + i)
  return (
    <div className="relative">
      <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span className="flex items-center gap-1"><span className="size-2.5 rounded-full bg-positive" />wallet up</span>
        <span className="flex items-center gap-1"><span className="size-2.5 rounded-full bg-negative" />wallet down</span>
        <span className="flex items-center gap-1"><span className="size-2.5 rounded-full border border-muted-foreground/50" />dead</span>
        <span className="flex items-center gap-1"><span className="size-2.5 rounded-full ring-2 ring-chart-1" />born last gen</span>
        <span className="flex items-center gap-1"><span className="size-2.5 animate-ping rounded-full ring-2 ring-chart-2" />trading now</span>
        <span>bigger = more trades · line = parent → child · dashed = crossover mate</span>
        <button type="button" onClick={() => setShowDead(v => !v)} className="ml-auto rounded border px-2 py-0.5 hover:bg-muted">
          {showDead ? "hide dead" : "show dead"}
        </button>
      </div>
      <div className="overflow-x-auto rounded-lg border bg-muted/20">
        <svg width={layout.width} height={layout.height} className="block" onMouseLeave={() => setHover(null)}>
          {layout.lanes.map((l, i) => (
            <g key={l.id}>
              {i % 2 === 0 && <rect x={0} y={l.top} width={layout.width} height={l.h} fill="var(--muted)" opacity={0.35} />}
              <text x={8} y={l.top + ROW_H / 2 + 4} fontSize={11} fill="var(--muted-foreground)">{l.id.length > 16 ? l.id.slice(0, 15) + "…" : l.id}</text>
            </g>
          ))}
          {gens.map(g => (g - layout.minGen) % 2 === 0 && (
            <text key={g} x={PAD_L + (g - layout.minGen) * GEN_W + GEN_W / 2} y={13} fontSize={10} textAnchor="middle" fill="var(--muted-foreground)">{g}</text>
          ))}
          {layout.edges.map(({ from, to, mate }, i) => {
            const on = lineage.has(to.a.key) && lineage.has(from.a.key)
            const alive = to.a.status === "swarm"
            const mx = (from.x + to.x) / 2
            return (
              <path key={i} d={`M${from.x},${from.y} C${mx},${from.y} ${mx},${to.y} ${to.x},${to.y}`} fill="none"
                stroke={on ? "var(--chart-1)" : "var(--muted-foreground)"} strokeWidth={on ? 2 : 1}
                strokeDasharray={mate ? "3 3" : undefined} opacity={on ? 0.95 : alive ? 0.45 : 0.15} />
            )
          })}
          {layout.nodes.map(n => {
            const a = n.a
            const fresh = a.status === "swarm" && a.gen >= lastGen
            const dim = hover != null && !lineage.has(a.key)
            return (
              <g key={a.key} transform={`translate(${n.x},${n.y})`} className="cursor-pointer" opacity={dim ? 0.35 : 1}
                onMouseEnter={() => setHover(n)} onClick={() => onPick(a)}>
                {fresh && <circle r={n.r + 3} fill="none" stroke="var(--chart-1)" strokeWidth={1.5} className="animate-pulse" />}
                {flash.has(a.key) && <circle r={n.r + 2} fill="none" stroke={flash.get(a.key)} strokeWidth={2} className="animate-ping"
                  style={{ transformBox: "fill-box", transformOrigin: "center" }} />}
                {a.status === "swarm" && a.equity >= 2 && <circle r={n.r + 5} fill="var(--positive)" opacity={0.15} className="animate-pulse" />}
                <circle r={n.r} fill={a.status === "dead" ? "transparent" : fill(a)} fillOpacity={strength(a)}
                  stroke={fill(a)} strokeWidth={a.status === "dead" ? 1 : 1.5} strokeOpacity={a.status === "dead" ? 0.5 : 1} />
                {a.status === "dead" && <path d={`M${-n.r / 2},${-n.r / 2} L${n.r / 2},${n.r / 2} M${n.r / 2},${-n.r / 2} L${-n.r / 2},${n.r / 2}`} stroke="var(--muted-foreground)" strokeWidth={1} opacity={0.6} />}
              </g>
            )
          })}
        </svg>
      </div>
      {hover && <MapTip n={hover} />}
    </div>
  )
}

function MapTip({ n }: { n: Node }) {
  const a = n.a
  return (
    <div className="pointer-events-none mt-2 rounded-lg border bg-popover p-3 text-xs shadow-sm sm:absolute sm:right-3 sm:top-10 sm:mt-0 sm:w-72">
      <div className="flex items-center justify-between gap-2">
        <span className="truncate font-medium">{a.species}<span className="text-muted-foreground">@{a.version.slice(0, 7)}</span></span>
        <Badge variant="outline">{a.status === "swarm" ? "alive" : a.status}</Badge>
      </div>
      <div className="num mt-2 grid grid-cols-3 gap-2">
        <div><div className="text-muted-foreground">wallet</div><div className={a.equity >= 1 ? "text-positive" : "text-negative"}>{pct(a.equity * 100, 0)}</div></div>
        <div><div className="text-muted-foreground">trades</div>{a.live_n} <span className="text-muted-foreground">({a.live_n ? Math.round(a.wins / a.live_n * 100) : 0}% won)</span></div>
        <div><div className="text-muted-foreground">gen</div>{a.gen}</div>
      </div>
      {a.ops.length > 0 && <div className="mt-2 text-muted-foreground">mutations: <span className="font-mono text-foreground">{a.ops.slice(0, 4).join(", ")}</span></div>}
      {a.reason && <div className="mt-1 text-negative [overflow-wrap:anywhere]">{a.reason}</div>}
      <div className="mt-1 text-muted-foreground">tap for the full profile</div>
    </div>
  )
}

// ── 3. what just happened: swarm events, council decisions and bans, by generation ─────
type Item = { ts: number; group: string; icon: Icon; tone: string; title: string; detail: string }

const EVENT_STYLE: Record<string, { icon: Icon; tone: string; verb: string }> = {
  born: { icon: Sparkles, tone: "text-chart-1", verb: "born" },
  killed: { icon: Skull, tone: "text-negative", verb: "killed" },
  thriving: { icon: TrendingUp, tone: "text-positive", verb: "thriving" },
  retired: { icon: Archive, tone: "text-muted-foreground", verb: "retired to make room" },
  promoted: { icon: Crown, tone: "text-chart-3", verb: "promoted to champion" },
}

export function ActivityFeed({ swarm, snap, limit = 40 }: { swarm: Swarm; snap: Snapshot; limit?: number }) {
  const items = useMemo(() => {
    const out: Item[] = swarm.events.map(e => {
      const st = EVENT_STYLE[e.event] ?? { icon: Dna, tone: "text-muted-foreground", verb: e.event }
      return { ts: e.ts, group: `Generation ${e.gen}`, icon: st.icon, tone: st.tone, title: `${e.key.split("@")[0]}@${(e.key.split("@")[1] ?? "").slice(0, 6)} ${st.verb}`,
        detail: [e.reason, e.ops.length ? `mutations: ${e.ops.slice(0, 3).join(", ")}` : ""].filter(Boolean).join(" · ") }
    })
    for (const i of swarm.council?.ideas ?? []) {
      out.push({ ts: i.ts, group: `Generation ${i.gen} · council`, icon: Lightbulb, tone: i.approved ? "text-chart-2" : "text-muted-foreground",
        title: `${i.proposed_by} proposed: ${i.kind} ${i.feature} ${i.op} ${num(i.v, 2)}`,
        detail: `${i.approved ? `approved (score ${num(i.score, 2)})` : "voted down or failed the replay check"} · now ${i.status}${i.status_reason ? `: ${i.status_reason}` : ""}` })
    }
    for (const b of snap.bans?.recent ?? []) {
      out.push({ ts: b.ts, group: "Ban list", icon: Ban, tone: "text-warning", title: `banned ${b.address.slice(0, 6)}… (${b.role})`, detail: b.reason })
    }
    return out.sort((x, y) => y.ts - x.ts).slice(0, limit)
  }, [swarm.events, swarm.council?.ideas, snap.bans?.recent, limit])
  if (!items.length) return <Empty>Nothing yet.</Empty>
  let lastGroup = ""
  return (
    <ol className="max-h-[30rem] space-y-1 overflow-y-auto pr-1">
      {items.map((it, k) => {
        const head = it.group !== lastGroup
        lastGroup = it.group
        const I = it.icon
        return (
          <li key={`${it.ts}-${k}`} className="animate-in fade-in slide-in-from-top-1">
            {head && <div className="sticky top-0 z-10 bg-card py-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{it.group} · {ago(it.ts, snap.now)}</div>}
            <div className="flex gap-2 rounded-md px-1 py-1 hover:bg-muted/50">
              <I className={cn("mt-0.5 size-4 shrink-0", it.tone)} />
              <div className="min-w-0 text-sm">
                <div className="font-medium [overflow-wrap:anywhere]">{it.title}</div>
                {it.detail && <div className="text-xs text-muted-foreground [overflow-wrap:anywhere]">{it.detail}</div>}
              </div>
            </div>
          </li>
        )
      })}
    </ol>
  )
}

// ── 4. the council chamber ─────────────────────────────────────────────────
/** `after` renders right under the chamber, above the latest sitting (the council page puts its chat there). */
export function CouncilChamber({ swarm, snap, after }: { swarm: Swarm; snap: Snapshot; after?: ReactNode }) {
  const c = swarm.council
  if (!c) return null
  const sitting = snap.swarm_clock?.running && snap.swarm_clock?.phase === "council"
  const ideas = c.ideas
  const lastGen = Math.max(-1, ...ideas.map(i => i.gen))
  const latest = ideas.filter(i => i.gen === lastGen)
  const count = (s: CouncilIdea["status"]) => ideas.filter(i => i.status === s).length
  const approved = ideas.filter(i => i.approved).length
  const funnel = [
    { label: "proposed", n: ideas.length, cls: "bg-chart-2/70" },
    { label: "approved", n: approved, cls: "bg-chart-2" },
    { label: "testing live", n: count("testing"), cls: "bg-chart-4" },
    { label: "working", n: count("working"), cls: "bg-positive" },
    { label: "didn't help", n: count("unhelpful") + count("failed"), cls: "bg-negative/70" },
  ]
  return (
    <div className="space-y-4">
      <Section
        title={<span className="flex items-center gap-2"><Scale className="size-4" />{sitting ? "The council is in session" : "The council chamber"}</span>}
        description={sitting ? "The judges are studying this generation's winners and voting on new rules right now."
          : `Meets during every swarm generation. ${c.members.filter(m => m.available).length} of ${c.members.length} judges available. A judge's vote weighs more when its past ideas worked live.`}>
        <div className="flex flex-wrap justify-center gap-4 sm:gap-8">
          {c.members.map(m => <Seat key={m.name} m={m} sitting={!!sitting} />)}
        </div>
        <div className="mt-5 space-y-1.5">
          {funnel.map(f => (
            <div key={f.label} className="flex items-center gap-2 text-xs">
              <span className="w-24 shrink-0 text-right text-muted-foreground">{f.label}</span>
              <div className="h-4 flex-1 overflow-hidden rounded bg-muted">
                <div className={cn("h-full rounded transition-[width] duration-700", f.cls)} style={{ width: `${ideas.length ? Math.max(f.n ? 3 : 0, f.n / ideas.length * 100) : 0}%` }} />
              </div>
              <span className="num w-8 shrink-0">{f.n}</span>
            </div>
          ))}
        </div>
      </Section>

      {after}

      <Section title={`Latest sitting${lastGen >= 0 ? ` · generation ${lastGen}` : ""}`}
        description="What each judge proposed, how the replay check scored it, and how every judge voted.">
        {latest.length === 0 ? <Empty>No sitting yet.</Empty> : (
          <div className="grid gap-3 lg:grid-cols-2">
            {latest.map(i => <Deliberation key={i.idea_id} i={i} members={c.members} />)}
          </div>
        )}
      </Section>
    </div>
  )
}

function Seat({ m, sitting }: { m: CouncilMember; sitting: boolean }) {
  const thinking = sitting && m.available && m.enabled
  const state = !m.enabled || !m.available ? "off" : m.last_error && m.ok === 0 ? "error" : "ok"
  const ring = state === "off" ? "var(--muted-foreground)" : state === "error" ? "var(--warning)" : "var(--positive)"
  const okRate = m.calls ? m.ok / m.calls : null
  return (
    <div className="flex w-36 flex-col items-center text-center">
      <div className="relative size-20">
        <svg viewBox="0 0 80 80" className={cn("absolute inset-0", thinking && "animate-pulse lg:animate-spin lg:[animation-duration:3s]")}>
          <circle cx={40} cy={40} r={36} fill="none" stroke="var(--border)" strokeWidth={4} />
          <circle cx={40} cy={40} r={36} fill="none" stroke={ring} strokeWidth={4} strokeLinecap="round"
            strokeDasharray={`${(thinking ? 0.3 : okRate ?? 0.05) * 226} 226`} transform="rotate(-90 40 40)" />
        </svg>
        <div className={cn("absolute inset-3 flex items-center justify-center rounded-full bg-muted", thinking && "animate-pulse")}>
          <Brain className={cn("size-7", state === "off" ? "text-muted-foreground" : "text-foreground")} />
        </div>
      </div>
      <div className="mt-1.5 truncate text-sm font-medium">{m.name}</div>
      <div className="w-full truncate font-mono text-[10px] text-muted-foreground">{m.model || "no model"}</div>
      <div className="num mt-1 text-xs">
        <span className="text-muted-foreground">vote</span> ×{m.weight.toFixed(2)} · <span className="text-positive">{m.working}</span>/<span>{m.proposed}</span> worked
      </div>
      <div className="text-[11px] text-muted-foreground">
        {thinking ? "deliberating…" : state === "off" ? (m.missing_key ? "needs its API key" : "offline")
          : `${okRate != null ? pct(okRate * 100, 0) + " answered" : "no calls"}${m.avg_s ? ` · ${Math.round(m.avg_s)}s` : ""}`}
      </div>
    </div>
  )
}

function Deliberation({ i, members }: { i: CouncilIdea; members: CouncilMember[] }) {
  const s = i.in_sample ?? {}
  const kept = s.kept ?? 0
  const blocked = s.blocked ?? 0
  const tot = Math.max(1, kept + blocked)
  return (
    <div className="rounded-lg border p-3 text-sm">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="text-xs text-muted-foreground">{i.proposed_by} proposes, for {i.strategy_id}</div>
          <div className="font-mono text-[13px] [overflow-wrap:anywhere]">{i.kind} {i.feature} {i.op} {num(i.v, 2)}</div>
        </div>
        <Badge variant="outline" className={cn("shrink-0", i.status === "working" && "text-positive", i.status === "testing" && "text-chart-2",
          (i.status === "failed" || i.status === "unhelpful") && "text-negative")}>{i.status}</Badge>
      </div>
      {i.reason && <p className="mt-1.5 text-xs italic text-muted-foreground [overflow-wrap:anywhere]">“{i.reason.slice(0, 220)}”</p>}
      {(kept || blocked) > 0 && (
        <div className="mt-2">
          <div className="flex h-2 overflow-hidden rounded bg-muted">
            <div className="bg-positive/70" style={{ width: `${kept / tot * 100}%` }} />
            <div className="bg-negative/60" style={{ width: `${blocked / tot * 100}%` }} />
          </div>
          <div className="num mt-1 text-[11px] text-muted-foreground">
            replay check: keeps {kept}, blocks {blocked} trades{s.saved_sol != null ? ` · would have saved ${signedSol(s.saved_sol)} SOL` : ""}{s.winners_lost ? ` · loses ${s.winners_lost} winners` : ""}
          </div>
        </div>
      )}
      <div className="mt-2 flex flex-wrap gap-1.5">
        {members.map(m => {
          const v = i.votes?.[m.name]
          return (
            <span key={m.name} title={v?.reason ?? "did not vote"}
              className={cn("inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px]",
                !v ? "text-muted-foreground/60" : v.approve ? "border-positive/40 bg-positive/10 text-positive" : "border-negative/40 bg-negative/10 text-negative")}>
              {m.name} {!v ? "–" : v.approve ? "✓" : "✗"}{v ? ` ${Math.round(v.confidence * 100)}%` : ""}
            </span>
          )
        })}
        {i.score != null && <span className="ml-auto text-[11px] text-muted-foreground">score {num(i.score, 2)}</span>}
      </div>
      {i.approved && (
        <div className="num mt-2 text-[11px] text-muted-foreground">
          live: {i.carriers} carrier{i.carriers === 1 ? "" : "s"} · {i.live_trades} trades · <span className={i.live_pnl_sol >= 0 ? "text-positive" : "text-negative"}>{signedSol(i.live_pnl_sol)} SOL</span>{i.deaths ? ` · ${i.deaths} died` : ""}
        </div>
      )}
    </div>
  )
}

