import { useMemo, useState } from "react"
import { Bar, CartesianGrid, ComposedChart, Line, LineChart, ReferenceLine, XAxis, YAxis } from "recharts"

import { Empty, RoleBadge, Section, ShowMore, SortChips, SortHead, Sparkline, Stat, StrategyName, useSort } from "@/components/app/bits"
import { Badge } from "@/components/ui/badge"
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart"
import { Progress } from "@/components/ui/progress"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ActivityFeed, GenerationPipeline, SwarmMap } from "@/components/app/visualizer"
import type { Agent, Snapshot, Swarm, TradesView } from "@/lib/api"
import { ago, dayClock, duration, fit, num, opLabel, pct, signedSol, splitKey, tone } from "@/lib/format"
import { cn } from "@/lib/utils"

const PHASE: Record<string, string> = {
  judging: "judging", learning: "learning", "learning from winners": "studying winners", breeding: "breeding",
  screening: "screening", "stress test": "stress testing", "ban scan": "ban scan", "whale scan": "whale scan",
  council: "council", done: "finishing",
}
const PHASE_HINT: Record<string, string> = {
  judging: "killing losers",
  learning: "replaying the last 24h",
  "learning from winners": "what the best agents have in common",
  breeding: "making children",
  screening: "replaying children, then they go live",
  "stress test": "worse fills, fees and delays on the winners",
  "ban scan": "looking for pump-and-dump wallets",
  "whale scan": "re-scoring smart wallets",
  council: "LLM judges studying winners (slots already refilled)",
  done: "writing the report",
}

export function SwarmView({ swarm, snap, trades }: { swarm: Swarm | null; snap: Snapshot; trades: TradesView | null }) {
  const [picked, setPicked] = useState<Agent | null>(null)
  const clock = snap.swarm_clock
  const agents = swarm?.agents ?? []
  const alive = useMemo(() => agents.filter(a => a.status === "swarm"), [agents])
  const dead = useMemo(() => agents.filter(a => a.status === "dead").sort((a, b) => (b.ended_ts ?? 0) - (a.ended_ts ?? 0)), [agents])
  const winRate = (a: Agent) => (a.live_n ? a.wins / a.live_n : null)
  const liv = useSort(alive, {
    gen: a => a.gen, fitness: a => a.fitness, bet: a => a.bet_pct, trades: a => a.live_n, win: winRate,
    pnl: a => a.pnl_sol, wallet: a => a.equity, agent: a => a.key,
  }, "swarm-alive")
  const grave = useSort(dead, {
    lived: a => (a.ended_ts ? a.ended_ts - a.born_ts : null), trades: a => a.live_n, pnl: a => a.pnl_sol, agent: a => a.key,
  }, "swarm-graveyard")
  if (!swarm) return <Empty>Loading the swarm…</Empty>
  if (swarm.error) return <Empty>Swarm data unavailable: {swarm.error}</Empty>

  const promoted = swarm.agents.filter(a => a.status === "champion" || (a.ended_ts && a.status === "retired"))
  const openByKey = new Map(snap.strategies.map(s => [s.key, s.open]))
  const best = alive[0]
  const now = snap.now
  const nextIn = clock?.next_ts ? clock.next_ts - now : null

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-5">
        <Stat label="Generation" value={swarm.generation || "—"} sub={swarm.last_ts ? `last ${ago(swarm.last_ts, now)}` : "none yet"} />
        <Stat label={clock?.running ? "Evolving now" : "Next generation"}
          value={clock?.running ? (PHASE[clock.phase ?? ""] ?? "starting…") : nextIn != null ? (nextIn > 0 ? duration(nextIn) : "due") : "—"}
          tone={clock?.running ? "text-chart-1" : undefined}
          sub={clock?.running ? `${clock.started_ts ? duration(now - clock.started_ts) : "just"} in · ${PHASE_HINT[clock.phase ?? ""] ?? "runs every " + swarm.config.every_min + " min"}` : `every ${swarm.config.every_min} min`} />
        <Stat label="Alive" value={`${alive.length} / ${swarm.config.population}`} sub={`${dead.length} dead · ${promoted.length} promoted`}>
          <Progress value={(alive.length / Math.max(1, swarm.config.population)) * 100} />
        </Stat>
        <Stat label="Swarm live PnL" value={signedSol(alive.reduce((a, x) => a + x.pnl_sol, 0))}
          tone={tone(alive.reduce((a, x) => a + x.pnl_sol, 0))} sub={`${alive.reduce((a, x) => a + x.live_n, 0)} closed trades`} />
        <Stat label="Fittest agent" value={best ? fit(best.fitness) : "—"} tone={tone(best?.fitness)}
          sub={best ? <StrategyName k={best.key} /> : "—"} />
      </div>

      <GenerationPipeline swarm={swarm} snap={snap} />

      <div className="grid gap-4 xl:grid-cols-5 xl:items-start">
        <Section className="xl:col-span-3" title="Swarm map"
          description="Every agent by generation (left to right) and family (rows), linked to its parent. Hover to trace a bloodline, tap to open it.">
          <SwarmMap swarm={swarm} onPick={setPicked} tape={trades?.tape} now={trades?.now} />
        </Section>
        <Section className="xl:col-span-2" title="What just happened" description="Births, deaths, council decisions and bans, newest first.">
          <ActivityFeed swarm={swarm} snap={snap} />
        </Section>
      </div>

      <Tabs defaultValue="alive">
        <TabsList>
          <TabsTrigger value="alive">Alive ({alive.length})</TabsTrigger>
          <TabsTrigger value="dead">Graveyard ({dead.length})</TabsTrigger>
        </TabsList>

        <TabsContent value="alive">
          <Section title="Living agents" description={`Each trades its own $50 paper wallet. Dies below ${pct(swarm.config.bust_frac * 100, 0)} of it, or from ${swarm.config.kill_min_trades ?? 4} trades on once it is losing with ${pct((swarm.config.kill_confidence ?? 0.8) * 100, 0)} confidence. Judged every generation. Tap an agent for details.`}>
            {alive.length === 0 ? <Empty>No agents alive. The next generation breeds new ones.</Empty> : (
            <>
              <div className="space-y-2 md:hidden">
                <SortChips {...liv} options={[["fitness", "Fitness"], ["pnl", "PnL"], ["wallet", "Wallet"], ["win", "Win"], ["trades", "Trades"], ["bet", "Bet"], ["gen", "Gen"]]} />
                {liv.sorted.map((a, i) => (
                  <button key={a.key} onClick={() => setPicked(a)} className="w-full min-w-0 rounded-lg border p-3 text-left">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="flex items-baseline gap-2"><span className="num text-xs text-muted-foreground">{i + 1}</span><StrategyName k={a.key} /></div>
                        {(openByKey.get(a.key) ?? 0) > 0 && <span className="text-xs text-chart-2">{openByKey.get(a.key)} open</span>}
                      </div>
                      <div className="shrink-0 text-right"><Sparkline points={a.curve} baseline={1} width={72} height={24} /></div>
                    </div>
                    <div className="mt-2"><Ops ops={a.ops} max={3} /></div>
                    <div className="num mt-2 grid grid-cols-4 gap-2 text-xs">
                      <div><div className="text-muted-foreground">fitness</div><div className={tone(a.fitness)}>{fit(a.fitness)}</div></div>
                      <div><div className="text-muted-foreground">trades</div><div>{a.live_n}{a.live_n ? <span className="text-muted-foreground"> · {pct((a.wins / a.live_n) * 100, 0)}</span> : null}</div></div>
                      <div><div className="text-muted-foreground">PnL SOL</div><div className={tone(a.pnl_sol)}>{signedSol(a.pnl_sol)}</div></div>
                      <div><div className="text-muted-foreground">bet</div><div>{a.bet_pct != null ? `${a.bet_pct}%` : "—"}</div></div>
                    </div>
                    <div className="mt-2"><WalletBar equity={a.equity} bust={swarm.config.bust_frac} wide /></div>
                  </button>
                ))}
              </div>
              <div className="hidden md:block">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-8">#</TableHead>
                    <SortHead k="agent" {...liv}>Agent</SortHead>
                    <TableHead>Mutations</TableHead>
                    <SortHead k="gen" right {...liv}>Gen</SortHead>
                    <SortHead k="fitness" right {...liv}>Fitness</SortHead>
                    <SortHead k="bet" right {...liv}>Bet</SortHead>
                    <SortHead k="trades" right {...liv}>Trades</SortHead>
                    <SortHead k="win" right {...liv}>Win</SortHead>
                    <SortHead k="pnl" right {...liv}>PnL SOL</SortHead>
                    <SortHead k="wallet" right {...liv}>Wallet</SortHead>
                    <TableHead className="w-28">Equity</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {liv.sorted.map((a, i) => (
                    <TableRow key={a.key} className="cursor-pointer" onClick={() => setPicked(a)}>
                      <TableCell className="num text-muted-foreground">{i + 1}</TableCell>
                      <TableCell className="max-w-56">
                        <StrategyName k={a.key} />
                        {(openByKey.get(a.key) ?? 0) > 0 && <span className="ml-2 text-xs text-chart-2">{openByKey.get(a.key)} open</span>}
                      </TableCell>
                      <TableCell className="max-w-72"><Ops ops={a.ops} max={3} /></TableCell>
                      <TableCell className="num text-right">{a.gen}</TableCell>
                      <TableCell className={cn("num text-right", tone(a.fitness))}>{fit(a.fitness)}</TableCell>
                      <TableCell className="num text-right text-muted-foreground">{a.bet_pct != null ? `${a.bet_pct}%` : "—"}</TableCell>
                      <TableCell className="num text-right">{a.live_n}</TableCell>
                      <TableCell className="num text-right">{a.live_n ? pct((a.wins / a.live_n) * 100, 0) : "—"}</TableCell>
                      <TableCell className={cn("num text-right", tone(a.pnl_sol))}>{signedSol(a.pnl_sol)}</TableCell>
                      <TableCell className="text-right"><WalletBar equity={a.equity} bust={swarm.config.bust_frac} /></TableCell>
                      <TableCell><Sparkline points={a.curve} baseline={1} /></TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              </div>
            </>
            )}
          </Section>
        </TabsContent>

        <TabsContent value="dead">
          <Section title="Graveyard" description="Killed agents. Their mutations count against those kinds of change, and their genomes are never bred again.">
            {dead.length === 0 ? <Empty>Nobody has died yet. Judging starts after {swarm.config.min_live_trades} live trades.</Empty> : (
            <>
              <div className="space-y-2 md:hidden">
                <SortChips {...grave} options={[["pnl", "PnL"], ["trades", "Trades"], ["lived", "Lived"]]} />
                {grave.sorted.map(a => (
                  <button key={a.key} onClick={() => setPicked(a)} className="w-full min-w-0 rounded-lg border p-3 text-left">
                    <div className="flex items-baseline justify-between gap-2">
                      <StrategyName k={a.key} />
                      <span className={cn("num shrink-0 text-xs", tone(a.pnl_sol))}>{signedSol(a.pnl_sol)} SOL</span>
                    </div>
                    <div className="num mt-1 text-xs text-muted-foreground">lived {a.ended_ts ? duration(a.ended_ts - a.born_ts) : "—"} · {a.live_n} trades</div>
                    <p className="mt-1.5 text-xs text-negative/90 [overflow-wrap:anywhere]">{a.reason}</p>
                  </button>
                ))}
              </div>
              <div className="hidden md:block">
              <Table>
                <TableHeader>
                  <TableRow>
                    <SortHead k="agent" {...grave}>Agent</SortHead>
                    <TableHead>Mutations</TableHead>
                    <SortHead k="lived" right {...grave}>Lived</SortHead>
                    <SortHead k="trades" right {...grave}>Trades</SortHead>
                    <SortHead k="pnl" right {...grave}>PnL SOL</SortHead>
                    <TableHead>Cause of death</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {grave.sorted.map(a => (
                    <TableRow key={a.key} className="cursor-pointer" onClick={() => setPicked(a)}>
                      <TableCell className="max-w-56"><StrategyName k={a.key} /></TableCell>
                      <TableCell className="max-w-64"><Ops ops={a.ops} max={2} /></TableCell>
                      <TableCell className="num text-right">{a.ended_ts ? duration(a.ended_ts - a.born_ts) : "—"}</TableCell>
                      <TableCell className="num text-right">{a.live_n}</TableCell>
                      <TableCell className={cn("num text-right", tone(a.pnl_sol))}>{signedSol(a.pnl_sol)}</TableCell>
                      <TableCell className="max-w-80 truncate text-muted-foreground">{a.reason}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              </div>
            </>
            )}
          </Section>
        </TabsContent>

      </Tabs>

      <div className="grid gap-4 xl:grid-cols-5 xl:items-start">
        <EvolutionChart swarm={swarm} className="xl:col-span-3" />
        <Section className="xl:col-span-2" title="Strategy families" description="Best living agent vs. the family's champion (replay fitness)">
          <div className="space-y-3 xl:max-h-[22rem] xl:overflow-y-auto xl:pr-1">
            {swarm.species.length === 0 && <Empty>No families yet.</Empty>}
            {swarm.species.map(s => {
              const delta = s.best_fitness != null && s.champion_fitness != null && s.champion_fitness > -100 ? s.best_fitness - s.champion_fitness : null
              return (
                <div key={s.id} className="rounded-lg border p-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate font-medium">{s.id}</span>
                    <div className="flex gap-1.5 text-xs">
                      <Badge className="bg-chart-1/15 text-chart-1">{s.alive} alive</Badge>
                      {s.dead > 0 && <Badge className="bg-negative/15 text-negative">{s.dead} dead</Badge>}
                      {s.promoted > 0 && <Badge className="bg-chart-3/15 text-chart-3">{s.promoted} promoted</Badge>}
                    </div>
                  </div>
                  <div className="num mt-2 grid grid-cols-3 gap-2 text-xs text-muted-foreground">
                    <div>best <span className={cn("block text-sm", tone(s.best_fitness))}>{fit(s.best_fitness)}</span></div>
                    <div>champion <span className="block text-sm text-foreground">{s.champion ? fit(s.champion_fitness) : "trial"}</span></div>
                    <div>edge <span className={cn("block text-sm", tone(delta))}>{num(delta, 1, true)}</span></div>
                  </div>
                </div>
              )
            })}
          </div>
        </Section>
      </div>

      <AgentSheet agent={picked} swarm={swarm} onClose={() => setPicked(null)} />
    </div>
  )
}

export function LearningPage({ swarm }: { swarm: Swarm | null }) {
  if (!swarm) return <Empty>Loading the swarm…</Empty>
  if (swarm.error) return <Empty>Swarm data unavailable: {swarm.error}</Empty>
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
        <Stat label="Lessons" value={swarm.lessons.length} sub="losing entry conditions, now rules" />
        <Stat label="Saved by lessons" value={`${swarm.lessons.reduce((a, l) => a + l.saved_sol, 0).toFixed(3)} SOL`} tone="text-positive" sub="in replay, had they applied" />
        <Stat label="Mutation kinds judged" value={swarm.credit.length} sub="scored by how children did" />
        <Stat label="Events" value={swarm.events.length} sub="births, deaths and promotions" />
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
            <Section title="Lessons" description="Entry conditions that lost money. Children get them as “don't buy when…” rules.">
              {swarm.lessons.length === 0 ? <Empty>No lessons yet (needs a dozen trades per family).</Empty> : (
                <ShowMore items={swarm.lessons} n={10}>{shown => (
                <div className="space-y-2">
                  {shown.map((l, i) => {
                    const max = Math.max(...swarm.lessons.map(x => x.saved_sol))
                    return (
                      <div key={i} className="rounded-lg border p-3">
                        <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground">
                          <span>{l.strategy_id}</span>
                          <span className="num">blocks {l.blocked}/{l.of} trades</span>
                        </div>
                        <div className="mt-1 font-mono text-sm">don't buy when {l.feature} {l.op} {Number(l.v.toPrecision(4))}</div>
                        <div className="mt-2 flex items-center gap-2">
                          <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
                            <div className="h-full rounded-full bg-positive" style={{ width: `${(l.saved_sol / max) * 100}%` }} />
                          </div>
                          <span className="num w-24 text-right text-xs text-positive">saves {l.saved_sol.toFixed(3)} SOL</span>
                        </div>
                      </div>
                    )
                  })}
                </div>
                )}</ShowMore>
              )}
            </Section>
            <Section title="What mutations have done" description="Average fitness change of children over their parents, by kind of change. Deaths count as a big minus. Weight is how often it gets tried next.">
              {swarm.credit.length === 0 ? <Empty>No mutations judged yet.</Empty> : <CreditChart credit={swarm.credit} />}
            </Section>
          </div>
      <Section title="Timeline" description="Births, deaths and promotions, newest first">
            {swarm.events.length === 0 ? <Empty>Nothing yet.</Empty> : (
              <ShowMore items={swarm.events} n={20}>{shown => (
              <ol className="relative space-y-3 border-l pl-5">
                {shown.map((e, i) => (
                  <li key={i} className="relative">
                    <span className={cn("absolute -left-[25px] top-1.5 size-2.5 rounded-full ring-4 ring-card",
                      e.event === "born" ? "bg-chart-1" : e.event === "killed" ? "bg-negative" : "bg-chart-3")} />
                    <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
                      <span className="font-medium capitalize">{e.event}</span>
                      <StrategyName k={e.key} />
                      <span className="text-xs text-muted-foreground">gen {e.gen} · {dayClock(e.ts)}</span>
                    </div>
                    {e.event === "born" ? (
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                        <Ops ops={e.ops} max={4} />
                        <span className="num">replay {fit(e.fitness)} vs parent {fit(e.parent_fitness)}</span>
                      </div>
                    ) : <p className="mt-1 text-xs text-muted-foreground">{e.reason}</p>}
                  </li>
                ))}
              </ol>
              )}</ShowMore>
            )}
          </Section>
    </div>
  )
}

function Ops({ ops, max }: { ops: string[]; max: number }) {
  if (!ops.length) return <span className="text-xs text-muted-foreground">—</span>
  return (
    <div className="flex flex-wrap gap-1">
      {ops.slice(0, max).map((o, i) => (
        <Badge key={i} variant="outline" className={cn("font-mono text-[11px] font-normal", o.startsWith("lesson") && "border-positive/40 text-positive")}>{opLabel(o)}</Badge>
      ))}
      {ops.length > max && <span className="text-xs text-muted-foreground">+{ops.length - max}</span>}
    </div>
  )
}

function WalletBar({ equity, bust, wide }: { equity: number; bust: number; wide?: boolean }) {
  const v = Math.max(0, Math.min(2, equity))
  return (
    <div className={cn("flex items-center gap-2", wide ? "w-full" : "ml-auto w-28")}>
      <div className="relative h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
        <div className="absolute inset-y-0 w-px bg-negative/70" style={{ left: `${(bust / 2) * 100}%` }} />
        <div className="absolute inset-y-0 w-px bg-muted-foreground/60" style={{ left: "50%" }} />
        <div className={cn("h-full rounded-full", equity >= 1 ? "bg-positive" : equity < bust * 1.5 ? "bg-negative" : "bg-warning")} style={{ width: `${(v / 2) * 100}%` }} />
      </div>
      <span className={cn("num w-10 text-right text-xs", tone(equity - 1))}>{pct(equity * 100, 0)}</span>
    </div>
  )
}

function EvolutionChart({ swarm, className }: { swarm: Swarm; className?: string }) {
  const data = swarm.generations.map(g => ({ ...g, killedNeg: -g.killed }))
  const config = {
    best: { label: "Best fitness", color: "var(--chart-1)" },
    median: { label: "Median fitness", color: "var(--chart-2)" },
    born: { label: "Born", color: "var(--chart-1)" },
    killedNeg: { label: "Killed", color: "var(--negative)" },
  } satisfies ChartConfig
  return (
    <Section className={className} title="Evolution" description="Fitness of the population per generation (lines) and births vs. deaths (bars)">
      {data.length === 0 ? <Empty>The first generation runs ~5 minutes after the bot starts.</Empty> : (
        <ChartContainer config={config} className="aspect-auto h-[22rem] w-full">
          <ComposedChart data={data} margin={{ left: 4, right: 8, top: 8 }}>
            <CartesianGrid vertical={false} />
            <XAxis dataKey="gen" tickLine={false} axisLine={false} tickFormatter={g => `G${g}`} />
            <YAxis yAxisId="fit" tickLine={false} axisLine={false} width={40} />
            <YAxis yAxisId="n" orientation="right" tickLine={false} axisLine={false} width={28} tickFormatter={v => String(Math.abs(v))} />
            <ReferenceLine yAxisId="fit" y={0} stroke="var(--muted-foreground)" strokeDasharray="4 4" />
            <ChartTooltip content={<ChartTooltipContent labelFormatter={(_, p) => `Generation ${p?.[0]?.payload?.gen}`}
              formatter={(v, n) => (
                <div className="flex w-full justify-between gap-4">
                  <span className="text-muted-foreground">{config[n as keyof typeof config]?.label ?? n}</span>
                  <span className="num">{n === "killedNeg" ? Math.abs(Number(v)) : n === "born" ? v : num(Number(v), 1, true)}</span>
                </div>
              )} />} />
            <Bar yAxisId="n" dataKey="born" fill="var(--color-born)" fillOpacity={0.35} radius={[3, 3, 0, 0]} maxBarSize={22} />
            <Bar yAxisId="n" dataKey="killedNeg" fill="var(--color-killedNeg)" fillOpacity={0.45} radius={[0, 0, 3, 3]} maxBarSize={22} />
            <Line yAxisId="fit" dataKey="best" stroke="var(--color-best)" strokeWidth={2.25} dot={{ r: 3 }} isAnimationActive={false} connectNulls />
            <Line yAxisId="fit" dataKey="median" stroke="var(--color-median)" strokeWidth={1.75} strokeDasharray="5 4" dot={false} isAnimationActive={false} connectNulls />
          </ComposedChart>
        </ChartContainer>
      )}
    </Section>
  )
}

function CreditChart({ credit }: { credit: Swarm["credit"] }) {
  const rows = credit.slice(0, 16)
  const max = Math.max(1, ...rows.map(r => Math.abs(r.mean_delta)))
  return (
    <div className="space-y-1.5">
      {rows.map((r, i) => (
        <div key={i} className="grid grid-cols-[minmax(0,1fr)_8rem_3.5rem] items-center gap-3 text-xs">
          <div className="min-w-0">
            <div className="truncate font-mono">{opLabel(r.kind)}</div>
            <div className="truncate text-muted-foreground">{r.strategy_id} · {r.n} tries</div>
          </div>
          <div className="relative h-2 rounded-full bg-muted">
            <div className="absolute inset-y-0 left-1/2 w-px bg-muted-foreground/50" />
            <div className={cn("absolute inset-y-0 rounded-full", r.mean_delta >= 0 ? "left-1/2 bg-positive" : "right-1/2 bg-negative")}
              style={{ width: `${(Math.abs(r.mean_delta) / max) * 50}%` }} />
          </div>
          <span className={cn("num text-right", tone(r.mean_delta))}>{num(r.mean_delta, 1, true)}</span>
        </div>
      ))}
    </div>
  )
}

function AgentSheet({ agent, swarm, onClose }: { agent: Agent | null; swarm: Swarm; onClose: () => void }) {
  const parentKey = agent?.parent ? `${agent.species}@${agent.parent}` : null
  const parent = useMemo(() => swarm.agents.find(a => a.key === parentKey) ?? null, [swarm.agents, parentKey])
  const config = { eq: { label: "Wallet", color: "var(--chart-1)" } } satisfies ChartConfig
  return (
    <Sheet open={agent != null} onOpenChange={o => { if (!o) onClose() }}>
      <SheetContent className="overflow-y-auto data-[side=right]:w-full data-[side=right]:sm:max-w-2xl">
        {agent && (
          <>
            <SheetHeader>
              <SheetTitle className="flex items-center gap-2"><StrategyName k={agent.key} /><RoleBadge role={agent.status} /></SheetTitle>
              <SheetDescription>
                Born in generation {agent.gen}, {dayClock(agent.born_ts)}
                {agent.ended_ts ? ` · ${agent.status === "dead" ? "died" : "promoted"} ${dayClock(agent.ended_ts)}` : ""}
              </SheetDescription>
            </SheetHeader>
            <div className="space-y-5 px-4 pb-6">
              {agent.reason && <p className={cn("rounded-lg p-3 text-sm", agent.status === "dead" ? "bg-negative/10 text-negative" : "bg-chart-3/10 text-chart-3")}>{agent.reason}</p>}
              <div className="grid grid-cols-3 gap-3 text-sm">
                <Mini label="Fitness" value={fit(agent.fitness)} className={tone(agent.fitness)} />
                <Mini label="Replay" value={`${fit(agent.replay)} · ${agent.replay_n ?? 0} trades`} />
                <Mini label="Live PnL" value={`${signedSol(agent.pnl_sol)} SOL`} className={tone(agent.pnl_sol)} />
                <Mini label="Trades" value={String(agent.live_n)} />
                <Mini label="Win rate" value={agent.live_n ? pct((agent.wins / agent.live_n) * 100, 0) : "—"} />
                <Mini label="Wallet" value={pct(agent.equity * 100, 0)} className={tone(agent.equity - 1)} />
                <Mini label="Bet size" value={agent.bet_pct != null ? `${agent.bet_pct}% of wallet` : "fixed"} />
                <Mini label="Generation" value={String(agent.gen)} />
                <Mini label="Age" value={duration((agent.ended_ts ?? Date.now() / 1000) - agent.born_ts)} />
              </div>

              <div>
                <h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Wallet over time</h4>
                {agent.curve.length < 2 ? <Empty>No closed trades yet.</Empty> : (
                  <ChartContainer config={config} className="aspect-auto h-44 w-full">
                    <LineChart data={agent.curve.map(([t, v]) => ({ t, eq: v * 100 }))} margin={{ left: 4, right: 8, top: 8 }}>
                      <CartesianGrid vertical={false} />
                      <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} tickFormatter={dayClock} tickLine={false} axisLine={false} minTickGap={60} />
                      <YAxis tickFormatter={v => `${Math.round(v)}%`} tickLine={false} axisLine={false} width={40} domain={[0, "auto"]} />
                      <ReferenceLine y={100} stroke="var(--muted-foreground)" strokeDasharray="4 4" />
                      <ReferenceLine y={swarm.config.bust_frac * 100} stroke="var(--negative)" strokeDasharray="4 4" label={{ value: "bust", position: "insideBottomLeft", fill: "var(--negative)", fontSize: 11 }} />
                      <ChartTooltip content={<ChartTooltipContent labelFormatter={(_, p) => dayClock(p?.[0]?.payload?.t ?? 0)} formatter={v => `${Number(v).toFixed(1)}%`} />} />
                      <Line dataKey="eq" type="stepAfter" stroke="var(--color-eq)" strokeWidth={2} dot={false} isAnimationActive={false} />
                    </LineChart>
                  </ChartContainer>
                )}
              </div>

              <div>
                <h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Lineage</h4>
                <div className="space-y-2 text-sm">
                  <div className="flex items-center justify-between rounded-lg border p-2.5">
                    <span className="text-muted-foreground">Parent</span>
                    <span className="font-mono text-xs">{agent.parent ? `${agent.species}@${agent.parent}` : "—"}{parent ? ` (gen ${parent.gen}, ${parent.status})` : agent.parent ? " (seed)" : ""}</span>
                  </div>
                  {agent.mate && (
                    <div className="flex items-center justify-between rounded-lg border p-2.5">
                      <span className="text-muted-foreground">Crossed with</span>
                      <span className="font-mono text-xs">{agent.species}@{agent.mate}</span>
                    </div>
                  )}
                  <div className="rounded-lg border p-2.5">
                    <div className="mb-2 text-muted-foreground">Changes from parent <span className="num">(replay {fit(agent.parent_fitness)} → {fit(agent.replay)})</span></div>
                    <Ops ops={agent.ops} max={10} />
                  </div>
                </div>
              </div>

              <div>
                <h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Recent trades</h4>
                {agent.trades.length === 0 ? <Empty>No closed trades yet.</Empty> : (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Coin</TableHead>
                        <TableHead>Exit</TableHead>
                        <TableHead className="text-right">Held</TableHead>
                        <TableHead className="text-right">Peak</TableHead>
                        <TableHead className="text-right">PnL</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {agent.trades.map((t, i) => (
                        <TableRow key={i}>
                          <TableCell className="max-w-28 truncate">{t.symbol || splitKey(t.mint)[0].slice(0, 6)}</TableCell>
                          <TableCell className="text-xs text-muted-foreground">{t.exit_reason ?? "—"}</TableCell>
                          <TableCell className="num text-right">{duration(t.hold_s)}</TableCell>
                          <TableCell className="num text-right">{t.mfe_x ? `${t.mfe_x.toFixed(2)}x` : "—"}</TableCell>
                          <TableCell className={cn("num text-right", tone(t.pnl_sol))}>{signedSol(t.pnl_sol)} <span className="text-xs">({pct(t.pnl_pct, 0, true)})</span></TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </div>
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  )
}

function Mini({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <div className="rounded-lg border p-2.5">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={cn("num mt-0.5 font-medium", className)}>{value}</div>
    </div>
  )
}
