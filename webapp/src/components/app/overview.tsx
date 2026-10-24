import { useMemo } from "react"
import { Area, AreaChart, CartesianGrid, Line, LineChart, ReferenceLine, XAxis, YAxis } from "recharts"

import { BestStat } from "@/components/app/best"
import { Empty, RoleBadge, Section, SortChips, SortHead, Sparkline, Stat, StrategyName, useSort } from "@/components/app/bits"
import { groupPositions } from "@/components/app/market"
import { CoinLink } from "@/components/app/narratives"
import { MoreLink } from "@/components/app/shell"
import { TapeRow } from "@/components/app/trades"
import { ActivityFeed } from "@/components/app/visualizer"
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart"
import { Progress } from "@/components/ui/progress"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import type { History, SessionReport, Snapshot, Swarm, TradesView } from "@/lib/api"
import { ago, clock, clockFor, duration, int, pct, signedSol, signedUsd, sol, splitKey, tone, usd } from "@/lib/format"
import { useInspect } from "@/lib/inspect"
import { cn } from "@/lib/utils"

const LINE_COLORS = ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)", "var(--chart-4)", "var(--chart-5)",
  "oklch(0.75 0.12 200)", "oklch(0.8 0.1 120)", "oklch(0.72 0.14 340)"]

export function Overview({ snap, history, swarm, trades }: {
  snap: Snapshot; history: History | null; swarm: Swarm | null; trades: TradesView | null
}) {
  const w = snap.wallet
  const closed = snap.strategies.reduce((a, s) => a + s.closed, 0)
  const open = snap.strategies.reduce((a, s) => a + s.open, 0)
  const alive = (swarm?.agents ?? []).filter(a => a.status === "swarm")
  const eq = alive.map(a => a.equity).sort((a, b) => a - b)
  const median = eq.length ? eq[Math.floor(eq.length / 2)] : null
  const startUsd = w?.start_usd ?? null

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-6">
        <Stat label="Champions' wallet" value={usd(w?.balance_usd)} tone={tone(w?.pnl_usd)}
          sub={<>
            <span className={tone(w?.pnl_usd)}>{signedUsd(w?.pnl_usd)} ({pct(w?.pnl_pct, 1, true)}) since {usd(w?.start_usd)}</span>
            {w?.locked_usd != null && (
              <span className="mt-1 block">
                locked in <span className={tone(w.locked_usd)}>{signedUsd(w.locked_usd)}</span> · open positions <span className={tone(w.open_usd)}>{signedUsd(w.open_usd)}</span>
              </span>
            )}
          </>} />
        <Stat label="Goal" value={usd(w?.goal_usd, 0)} sub={`champions' wallet is ${pct(w?.goal_pct, 0)} of the way`}>
          <Progress value={Math.max(0, Math.min(100, w?.goal_pct ?? 0))} />
        </Stat>
        <Stat label="Swarm wallets" value={median != null && startUsd ? usd(median * startUsd) : "—"}
          tone={tone(median != null ? median - 1 : null)}
          sub={eq.length ? <>median of {eq.length} · best <span className={tone(eq[eq.length - 1] - 1)}>{pct(eq[eq.length - 1] * 100, 0)}</span> · worst <span className={tone(eq[0] - 1)}>{pct(eq[0] * 100, 0)}</span></> : "no agents yet"} />
        <Stat label="Session range" value={w?.range ? `${usd(w.range.low)} – ${usd(w.range.high)}` : "—"}
          sub={w?.range ? <span className={tone(w.range.from_high_pct)}>champions {pct(w.range.from_high_pct, 1, true)} from the high</span> : null} />
        <Stat label="Trades" value={int(closed)} sub={`${open} open, all wallets, this session`} />
        <BestStat best={swarm?.best} />
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <WalletChart snap={snap} history={history} />
        <SwarmWalletsChart swarm={swarm} startUsd={startUsd} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2 2xl:grid-cols-3">
        <OpenPositionsCard snap={snap} />
        <Section title="Latest trades" description={trades?.stats ? `${trades.stats.buys} buys · ${trades.stats.closed} closed, ${trades.stats.won} won · ${signedSol(trades.stats.pnl_sol)} SOL` : "Every agent's fills, newest first"}
          action={<MoreLink page="trades" />}>
          {!trades ? <Empty>Loading…</Empty> : trades.tape.length === 0 ? <Empty>No trades yet this session.</Empty> : (
            <ol className="divide-y">
              {trades.tape.slice(0, 7).map(t => <TapeRow key={`${t.ts}-${t.key}-${t.side}-${t.mint}`} t={t} now={trades.now} compact />)}
            </ol>
          )}
        </Section>
        <SwarmCard snap={snap} swarm={swarm} />
        <TopStrategiesCard snap={snap} history={history} />
        {swarm && !swarm.error && (
          <Section className="lg:col-span-2 2xl:col-span-2" title="What just happened" description="Births, deaths, council decisions and bans, newest first."
            action={<MoreLink page="learning">timeline</MoreLink>}>
            <ActivityFeed swarm={swarm} snap={snap} limit={14} />
          </Section>
        )}
      </div>
    </div>
  )
}

/** The coins the bot holds, one line per coin, biggest gain or loss first. */
function OpenPositionsCard({ snap }: { snap: Snapshot }) {
  const groups = useMemo(() => groupPositions(snap.positions)
    .filter(g => g.open > 0)
    .sort((a, b) => Math.abs(b.gain) - Math.abs(a.gain)), [snap.positions])
  return (
    <Section title="Open positions" description={groups.length ? `${groups.length} coins held · worth now vs. put in` : "Coins the bot holds right now"}
      action={<MoreLink page="positions" />}>
      {groups.length === 0 ? <Empty>Nothing open.</Empty> : (
        <ul className="space-y-1">
          {groups.slice(0, 7).map(g => (
            <li key={g.mint || g.coin} className="flex items-center gap-3 rounded-md px-1 py-1.5 text-sm hover:bg-muted/40">
              <div className="min-w-0 flex-1">
                {g.mint ? <CoinLink mint={g.mint} className="truncate">{g.coin}</CoinLink> : <span className="font-medium">{g.coin}</span>}
                <div className="truncate text-xs text-muted-foreground">
                  {g.rows.length} position{g.rows.length > 1 ? "s" : ""}{g.champion ? " · champion holds it" : ""}
                </div>
              </div>
              <div className="num text-right">
                <div className={cn("font-medium", tone(g.gain))}>{g.money(g.now)}</div>
                <div className={cn("text-xs", tone(g.gain))}>{g.inv ? pct((g.gain / g.inv) * 100, 0, true) : "—"}</div>
              </div>
            </li>
          ))}
          {groups.length > 7 && <li className="pt-1 text-center text-xs text-muted-foreground">+{groups.length - 7} more coins</li>}
        </ul>
      )}
    </Section>
  )
}

const PHASE_LABEL: Record<string, string> = {
  judging: "judging", learning: "learning", "learning from winners": "studying winners", breeding: "breeding",
  screening: "screening", "stress test": "stress testing", "ban scan": "ban scan", "whale scan": "whale scan",
  council: "council in session", done: "finishing",
}

/** Where the swarm is in its cycle, and its five best wallets. */
function SwarmCard({ snap, swarm }: { snap: Snapshot; swarm: Swarm | null }) {
  const inspect = useInspect()
  const clock = snap.swarm_clock
  const alive = useMemo(() => (swarm?.agents ?? []).filter(a => a.status === "swarm").sort((a, b) => b.equity - a.equity), [swarm])
  const nextIn = clock?.next_ts ? clock.next_ts - snap.now : null
  const status = clock?.running
    ? `Generation ${(swarm?.generation ?? 0) + 1} · ${PHASE_LABEL[clock.phase ?? ""] ?? "starting"}`
    : `Generation ${swarm?.generation || "—"} trading${nextIn != null ? ` · next in ${nextIn > 0 ? duration(nextIn) : "a moment"}` : ""}`
  return (
    <Section title="Swarm" description={status} action={<MoreLink page="swarm" />}>
      {!swarm ? <Empty>Loading the swarm…</Empty> : alive.length === 0 ? <Empty>No agents alive.</Empty> : (
        <ul className="space-y-1">
          {alive.slice(0, 6).map((a, i) => (
            <li key={a.key}>
              <button type="button" onClick={() => inspect.agent(a.key)} className="flex w-full items-center gap-3 rounded-md px-1 py-1.5 text-left text-sm hover:bg-muted/40">
                <span className="num w-4 text-xs text-muted-foreground">{i + 1}</span>
                <div className="min-w-0 flex-1">
                  <StrategyName k={a.key} />
                  <div className="num text-xs text-muted-foreground">gen {a.gen} · {a.live_n} trades{a.live_n ? ` · ${Math.round(a.wins / a.live_n * 100)}% won` : ""}</div>
                </div>
                <Sparkline points={a.curve} baseline={1} width={64} height={22} />
                <span className={cn("num w-14 text-right font-medium", tone(a.equity - 1))}>{pct(a.equity * 100, 0)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </Section>
  )
}

function TopStrategiesCard({ snap, history }: { snap: Snapshot; history: History | null }) {
  const inspect = useInspect()
  const rows = useMemo(() => snap.strategies.filter(s => s.role !== "swarm")
    .sort((a, b) => (b.role === "champion" ? 1 : 0) - (a.role === "champion" ? 1 : 0) || b.pnl_sol - a.pnl_sol), [snap.strategies])
  return (
    <Section title="Strategies" description="Champions first, then by profit this session" action={<MoreLink page="strategies" />}>
      {rows.length === 0 ? <Empty>No strategies.</Empty> : (
        <ul className="space-y-1">
          {rows.slice(0, 6).map(s => (
            <li key={s.key}>
              <button type="button" onClick={() => inspect.agent(s.key)} className="flex w-full items-center gap-3 rounded-md px-1 py-1.5 text-left text-sm hover:bg-muted/40">
                <div className="min-w-0 flex-1">
                  <StrategyName k={s.key} />
                  <div className="mt-0.5 flex items-center gap-2 text-xs text-muted-foreground"><RoleBadge role={s.role} />{s.open} open · {s.closed} closed</div>
                </div>
                <Sparkline points={history?.runners[s.key] ?? []} baseline={0} width={64} height={22} />
                <span className={cn("num w-16 text-right font-medium", tone(s.made_usd))}>{signedUsd(s.made_usd)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </Section>
  )
}

export function StrategiesPage({ snap, history, report }: { snap: Snapshot; history: History | null; report: SessionReport | null }) {
  const inspect = useInspect()
  const nonSwarm = useMemo(() => snap.strategies.filter(s => s.role !== "swarm"), [snap.strategies])
  const byPnl = useMemo(() => [...nonSwarm].sort((a, b) => b.pnl_sol - a.pnl_sol), [nonSwarm])
  const strat = useSort(byPnl, {
    strategy: s => s.key, role: s => s.role, open: s => s.open, closed: s => s.closed, win: s => (s.closed ? s.win_pct : null),
    pnl: s => s.pnl_sol, usd: s => s.made_usd, held: s => s.balance_sol,
  }, "strategies")
  const count = (r: string) => nonSwarm.filter(s => s.role === r).length

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
        <Stat label="Champions" value={count("champion")} sub="trade the shared wallet" />
        <Stat label="Challengers" value={count("challenger")} sub="own $50 wallet each" />
        <Stat label="Trials" value={count("trial")} sub="own $50 wallet each" />
        <Stat label="Session PnL" value={signedUsd(nonSwarm.reduce((a, s) => a + (s.made_usd ?? 0), 0))}
          tone={tone(nonSwarm.reduce((a, s) => a + s.pnl_sol, 0))} sub={`${signedSol(nonSwarm.reduce((a, s) => a + s.pnl_sol, 0))} SOL, swarm excluded`} />
      </div>
      <Section title="Strategies" description={snap.shared_wallet
        ? "Champions trade the shared champions' wallet. Challengers and trials trade their own $50 copy. Swarm agents are on the Swarm page. Tap a row for its full profile."
        : "Every strategy trades its own copy of the wallet. Tap a row for its full profile."}>
        {nonSwarm.length === 0 ? <Empty>No strategies.</Empty> : (
          <>
            <div className="space-y-2 md:hidden">
              <SortChips {...strat} options={[["pnl", "PnL"], ["open", "Open"], ["closed", "Closed"], ["win", "Win"], ["role", "Role"]]} />
              {strat.sorted.map(s => (
                <button type="button" key={s.key} onClick={() => inspect.agent(s.key)} className="w-full min-w-0 rounded-lg border p-3 text-left">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 space-y-1">
                      <StrategyName k={s.key} />
                      <div className="flex items-center gap-2"><RoleBadge role={s.role} />{!s.in_wallet && <span className="text-xs text-muted-foreground">own wallet</span>}</div>
                    </div>
                    <Sparkline points={history?.runners[s.key] ?? []} baseline={0} width={72} height={24} />
                  </div>
                  <div className="num mt-2 grid grid-cols-4 gap-2 text-xs">
                    <div><div className="text-muted-foreground">open</div><div>{s.open}</div></div>
                    <div><div className="text-muted-foreground">closed</div><div>{s.closed}{s.closed ? <span className="text-muted-foreground"> · {pct(s.win_pct, 0)}</span> : null}</div></div>
                    <div><div className="text-muted-foreground">PnL SOL</div><div className={tone(s.pnl_sol)}>{signedSol(s.pnl_sol)}</div></div>
                    <div><div className="text-muted-foreground">PnL USD</div><div className={tone(s.made_usd)}>{signedUsd(s.made_usd)}</div></div>
                  </div>
                </button>
              ))}
            </div>
            <div className="hidden md:block">
              <Table>
                <TableHeader>
                  <TableRow>
                    <SortHead k="strategy" {...strat}>Strategy</SortHead>
                    <SortHead k="role" {...strat}>Role</SortHead>
                    <SortHead k="open" right {...strat}>Open</SortHead>
                    <SortHead k="closed" right {...strat}>Closed</SortHead>
                    <SortHead k="win" right {...strat}>Win</SortHead>
                    <SortHead k="pnl" right {...strat}>PnL SOL</SortHead>
                    <SortHead k="usd" right {...strat}>PnL USD</SortHead>
                    <SortHead k="held" right {...strat}>Held</SortHead>
                    <TableHead className="w-28">Session</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {strat.sorted.map(s => (
                    <TableRow key={s.key} className="cursor-pointer" onClick={() => inspect.agent(s.key)}>
                      <TableCell className="max-w-64"><StrategyName k={s.key} /></TableCell>
                      <TableCell>
                        <div className="flex items-center gap-2">
                          <RoleBadge role={s.role} />
                          {!s.in_wallet && <span className="text-xs text-muted-foreground">own wallet</span>}
                        </div>
                      </TableCell>
                      <TableCell className="num text-right">{s.open}</TableCell>
                      <TableCell className="num text-right">{s.closed}</TableCell>
                      <TableCell className="num text-right">{s.closed ? pct(s.win_pct, 0) : "—"}</TableCell>
                      <TableCell className={`num text-right ${tone(s.pnl_sol)}`}>{signedSol(s.pnl_sol)}</TableCell>
                      <TableCell className={`num text-right ${tone(s.made_usd)}`}>{signedUsd(s.made_usd)}</TableCell>
                      <TableCell className="num text-right text-muted-foreground">{sol(s.balance_sol)}</TableCell>
                      <TableCell><Sparkline points={history?.runners[s.key] ?? []} baseline={0} /></TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </>
        )}
      </Section>

      {report?.markdown && (
        <Section title="Session report" description={`Updated ${ago(report.updated)}`}>
          {report.note && <p className="mb-3 rounded-lg bg-muted/50 p-3 text-sm">{report.note}</p>}
          <pre className="max-h-[32rem] overflow-auto whitespace-pre-wrap font-mono text-xs leading-relaxed text-muted-foreground">{report.markdown}</pre>
        </Section>
      )}
    </div>
  )
}

function WalletChart({ snap, history }: { snap: Snapshot; history: History | null }) {
  const w = snap.wallet
  const data = useMemo(() => (history?.wallet ?? []).filter(p => p[1] != null).map(([t, u]) => ({ t, usd: u })), [history])
  const config = { usd: { label: "Wallet", color: "var(--chart-1)" } } satisfies ChartConfig
  const down = (w?.pnl_usd ?? 0) < 0
  const span = data.length ? data[data.length - 1].t - data[0].t : 0
  return (
    <Section title="Champions' wallet" description="One $50 wallet shared by every champion: cash plus open positions at market, this session. Swarm agents don't use it.">
      {data.length < 2 ? <Empty>Collecting points — the chart fills in every 15 seconds.</Empty> : (
        <ChartContainer config={config} className="aspect-auto h-64 w-full">
          <AreaChart data={data} margin={{ left: 4, right: 8, top: 8 }}>
            <defs>
              <linearGradient id="walletFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor={down ? "var(--negative)" : "var(--positive)"} stopOpacity={0.35} />
                <stop offset="95%" stopColor={down ? "var(--negative)" : "var(--positive)"} stopOpacity={0.02} />
              </linearGradient>
            </defs>
            <CartesianGrid vertical={false} />
            <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} tickFormatter={clockFor(span)} tickLine={false} axisLine={false} minTickGap={56} />
            <YAxis tickFormatter={v => usd(v)} tickLine={false} axisLine={false} width={56} domain={["auto", "auto"]} />
            {w?.start_usd && <ReferenceLine y={w.start_usd} stroke="var(--muted-foreground)" strokeDasharray="4 4" label={{ value: "start", position: "insideTopLeft", fill: "var(--muted-foreground)", fontSize: 11 }} />}
            <ChartTooltip content={<ChartTooltipContent labelFormatter={(_, p) => clock(p?.[0]?.payload?.t ?? 0)} formatter={v => usd(Number(v))} />} />
            <Area dataKey="usd" type="monotone" stroke={down ? "var(--negative)" : "var(--positive)"} strokeWidth={2} fill="url(#walletFill)" isAnimationActive={false} />
          </AreaChart>
        </ChartContainer>
      )}
    </Section>
  )
}

function SwarmWalletsChart({ swarm, startUsd }: { swarm: Swarm | null; startUsd: number | null }) {
  const { data, keys } = useMemo(() => {
    const alive = (swarm?.agents ?? []).filter(a => a.status === "swarm" && a.curve.length > 1)
      .sort((a, b) => Math.abs(b.equity - 1) - Math.abs(a.equity - 1)).slice(0, 8)
    const rows = new Map<number, Record<string, number>>()
    const now = Date.now() / 1000
    for (const a of alive) {
      for (const [t, v] of [...a.curve, [now, a.equity] as [number, number]]) {
        const row = rows.get(t) ?? { t }
        row[a.key] = v * 100
        rows.set(t, row)
      }
    }
    return { data: [...rows.values()].sort((a, b) => a.t - b.t), keys: alive.map(a => a.key) }
  }, [swarm])
  const config = Object.fromEntries(keys.map((k, i) => [k.replace(/[@.]/g, "_"), { label: splitKey(k).join(" "), color: LINE_COLORS[i % LINE_COLORS.length] }])) satisfies ChartConfig
  const bust = (swarm?.config.bust_frac ?? 0.25) * 100
  return (
    <Section title="Swarm wallets" description={`Each agent's own $${startUsd ? Math.round(startUsd) : 50} wallet from closed trades, since it was born. Agents that traded the most shown.`}>
      {data.length < 2 ? <Empty>No swarm agent has closed a trade yet.</Empty> : (
        <ChartContainer config={config} className="aspect-auto h-64 w-full">
          <LineChart data={data} margin={{ left: 4, right: 8, top: 8 }}>
            <CartesianGrid vertical={false} />
            <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} tickFormatter={clockFor(data[data.length - 1].t - data[0].t)} tickLine={false} axisLine={false} minTickGap={56} />
            <YAxis tickFormatter={v => `${Math.round(v)}%`} tickLine={false} axisLine={false} width={44} domain={[0, "auto"]} />
            <ReferenceLine y={100} stroke="var(--muted-foreground)" strokeDasharray="4 4" />
            <ReferenceLine y={bust} stroke="var(--negative)" strokeDasharray="4 4" label={{ value: "bust", position: "insideBottomLeft", fill: "var(--negative)", fontSize: 11 }} />
            <ChartTooltip content={<ChartTooltipContent labelFormatter={(_, p) => clock(p?.[0]?.payload?.t ?? 0)} formatter={(v, n) => (
              <div className="flex w-full justify-between gap-4"><span className="text-muted-foreground">{splitKey(String(n)).join(" ")}</span><span className="num">{Number(v).toFixed(0)}%</span></div>
            )} />} />
            {keys.map((k, i) => (
              <Line key={k} dataKey={k} name={k} type="stepAfter" stroke={LINE_COLORS[i % LINE_COLORS.length]} strokeWidth={1.75} dot={false} connectNulls isAnimationActive={false} />
            ))}
          </LineChart>
        </ChartContainer>
      )}
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {keys.map((k, i) => (
          <span key={k} className="inline-flex items-center gap-1.5"><span className="size-2 rounded-full" style={{ background: LINE_COLORS[i % LINE_COLORS.length] }} />{splitKey(k)[0]} <span className="font-mono text-[10px]">{splitKey(k)[1]}</span></span>
        ))}
      </div>
    </Section>
  )
}
