import { ArrowDownRight, ArrowUpRight, ChevronRight, CircleCheck, CircleDashed, ShieldCheck, TriangleAlert, XCircle } from "lucide-react"
import { Fragment, useMemo, useState, type ComponentType, type ReactNode } from "react"
import { Area, AreaChart, CartesianGrid, Line, LineChart, ReferenceLine, XAxis, YAxis } from "recharts"

import { Empty, RoleBadge, Section, SortChips, SortHead, Stat, StrategyName, useSort } from "@/components/app/bits"
import { CoinLink } from "@/components/app/narratives"
import { CouncilView } from "@/components/app/council"
import { WhaleTape } from "@/components/app/whale-tape"
import { MoreLink } from "@/components/app/shell"
import { Badge } from "@/components/ui/badge"
import { Table, TableBody, TableCell, TableHeader, TableRow } from "@/components/ui/table"
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart"
import type { DryRunTrade, DryRunView, LiveFill, LiveView, Snapshot, Swarm } from "@/lib/api"
import { ago, clock, clockFor, dayClock, duration, num, pct, sol, tone, usd } from "@/lib/format"
import { useInspect } from "@/lib/inspect"
import { cn } from "@/lib/utils"

// Live mode: the real-size wallet. For now a dry run: the champions' trades at real executable
// prices (Jupiter quotes), on a wallet the size of a real deposit. Nothing is signed or sent.

type Poll = { data: LiveView | null; ok: boolean }
type Started = Extract<LiveView, { started: true }>

/** The live view once the wallet runs, or the message to show instead. */
function useStarted(live: Poll): { d: Started | null; empty: ReactNode } {
  const data = live.data
  if (!data) return { d: null, empty: <Empty>Loading the live wallet…</Empty> }
  if (!data.started) {
    return {
      d: null, empty: <Empty>{data.error ?? (data.enabled
        ? "The live wallet starts with the bot's next restart: a fresh $50 that copies the champions at real prices."
        : "The live dry run is off (dryrun.enabled in config/settings.yaml).")}</Empty>,
    }
  }
  return { d: data, empty: null }
}

function Banner({ d, ok }: { d: Started; ok: boolean }) {
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-xl border border-negative/30 bg-negative/5 px-4 py-2.5 text-sm">
      <ShieldCheck className="size-4 text-negative" />
      <Badge className="bg-muted text-foreground">Attempt #{d.attempt ?? 1}</Badge>
      <span className="font-medium">Dry run: no real money yet.</span>
      <span className="text-muted-foreground">A real-size {usd(d.start_usd, 0)} wallet takes every champion trade at the price a real swap would get right then. Nothing is signed or sent.</span>
      {!ok && <span className="text-xs text-negative">Can't reach the bot; showing the last numbers.</span>}
    </div>
  )
}

// ── overview ─────────────────────────────────────────────────────────────────
export function LivePage({ snap, live }: { snap: Snapshot; live: Poll }) {
  const { d, empty } = useStarted(live)
  if (!d) return empty
  const px = d.sol_usd ?? snap.sol_usd
  const gap = d.pnl_pct != null && d.paper_pnl_pct != null ? d.pnl_pct - d.paper_pnl_pct : null
  const c = d.counts
  const passed = d.readiness.filter(r => r.ok).length
  return (
    <div className="space-y-4">
      <Banner d={d} ok={live.ok} />
      {d.stopped && (
        <div className="rounded-xl border border-negative/50 bg-negative/10 px-4 py-3 text-sm">
          <div className="font-semibold text-negative">Stopped: attempt #{d.attempt} {d.stopped.reason}.</div>
          <p className="mt-1 text-muted-foreground">A real wallet with a kill switch would have stopped here, so this one stopped buying too
            (open positions still sell as the champion sells). Everything it measured keeps teaching paper and the council.</p>
          <NewAttempt next={d.attempt + 1} className="mt-2" />
        </div>
      )}
      <LearningLine d={d} />
      <RightNow snap={snap} d={d} />
      <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-5">
        <Tile label="Live wallet" value={usd(d.equity_usd)} t={tone(d.pnl_pct)}
              sub={<><span className={tone(d.pnl_pct)}>{pct(d.pnl_pct, 1, true)}</span> since {usd(d.start_usd, 0)} · {sol(d.equity_sol, 4)} SOL</>} />
        <Tile label="Paper wallet, same period" value={pct(d.paper_pnl_pct, 1, true)} t={tone(d.paper_pnl_pct)}
              sub={gap != null ? <>live vs paper <span className={tone(gap)}>{num(gap, 1, true)} pts</span></> : "—"} />
        <Tile label="Trades" value={String(d.trades)} sub={<>{d.won} won ({d.trades ? pct((100 * d.won) / d.trades, 0) : "—"}) · {d.open.length} open</>} />
        <Tile label="Failed" value={String(c.failed_buys + c.stuck)}
              sub={<>{c.no_route} no route · {c.slippage} slippage · <span className={c.stuck ? "text-negative" : ""}>{c.stuck} unsellable</span></>} />
        <Tile label="Cost of real prices" value={d.avg_buy_impact_pct != null ? `${num(d.avg_buy_impact_pct, 2)}%` : "—"}
              sub={<>price impact per buy · entry {d.avg_entry_gap_pct != null ? <span className={tone(-d.avg_entry_gap_pct)}>{num(d.avg_entry_gap_pct, 1, true)}%</span> : "—"} vs paper</>} />
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <LiveChart d={d} px={px} className="xl:col-span-2" />
        <Section title="Ready for real money?" description={`${passed} of ${d.readiness.length} checks pass. All of them should, for a while, before a real deposit.`}>
          <ul className="space-y-2.5">
            {d.readiness.map(r => (
              <li key={r.id} className="flex gap-2.5">
                {r.ok ? <CircleCheck className="mt-0.5 size-4 shrink-0 text-positive" /> : <CircleDashed className="mt-0.5 size-4 shrink-0 text-muted-foreground" />}
                <div className="min-w-0">
                  <div className={cn("text-sm font-medium", !r.ok && "text-muted-foreground")}>{r.label}</div>
                  <div className="text-xs text-muted-foreground">{r.detail}</div>
                </div>
              </li>
            ))}
          </ul>
          <p className="mt-3 text-[11px] text-muted-foreground">Still not modeled: quotes are taken the moment paper fills (a real transaction lands a second or two later), and other bots front-running us. Expect real results a little worse than this.</p>
        </Section>
      </div>

      <AttemptsHistory d={d} />

      <div className="grid gap-4 xl:grid-cols-2">
        <Section title="Open positions" description={d.open.length ? `${d.open.length} coins, worth what a real sell returns now` : "Nothing open"} action={<MoreLink page="live-positions" />}>
          {d.open.length === 0 ? <Empty>No open positions. The next champion buy shows up here.</Empty> : <PositionsTable d={d} px={px} snap={snap} limit={5} />}
        </Section>
        <Section title="Latest" description="Newest live fills and failed attempts" action={<MoreLink page="live-trades" />}>
          {d.tape.length === 0 ? <Empty>No trades yet. The first one comes with the champions' next buy.</Empty> : (
            <ol className="divide-y">{d.tape.slice(0, 8).map((t, i) => <FillRow key={`${t.ts}-${i}`} t={t} px={px} now={d.now} compact />)}</ol>
          )}
        </Section>
      </div>
    </div>
  )
}

// ── positions ────────────────────────────────────────────────────────────────
type Row = {
  key: string; mint: string; symbol: string; status: "open" | "sold" | "stuck"; cost: number; now: number
  paperPct: number | null; path?: [number, number][]; since: number; until: number | null; why: string
  impact: number | null; route: string; fails: number
  open?: DryRunView["open"][number]
}

export function LivePositionsPage({ snap, live }: { snap: Snapshot; live: Poll }) {
  const { d, empty } = useStarted(live)
  const [state, setState] = useState<"open" | "all">("open")
  const [openRows, setOpenRows] = useState<Set<string>>(new Set())
  const rows: Row[] = useMemo(() => {
    if (!d) return []
    const paper = new Map(snap.positions.filter(p => p.role === "champion").map(p => [p.mint, p]))
    const open: Row[] = d.open.map(p => {
      const pp = paper.get(p.mint)
      return { key: `o:${p.mint}`, mint: p.mint, symbol: p.symbol, status: "open", cost: p.cost_sol, now: p.value_sol,
               paperPct: pp && pp.invested_sol ? (pp.worth_sol / pp.invested_sol - 1) * 100 : null, path: p.path,
               since: p.entry_ts, until: null, why: "", impact: p.impact_pct, route: p.route, fails: p.sell_fails, open: p }
    })
    const closed: Row[] = d.recent.filter(t => t.status !== "missed").map((t, i) => ({
      key: `c:${t.mint}:${t.exit_ts}:${i}`, mint: t.mint, symbol: t.symbol, status: t.status === "stuck" ? "stuck" : "sold",
      cost: t.size_sol, now: t.proceeds_sol, paperPct: t.paper_pnl_pct, path: t.path, since: t.entry_ts, until: t.exit_ts,
      why: t.status === "stuck" ? "couldn't sell" : t.exit_reason, impact: t.buy_impact_pct, route: t.route, fails: 0,
    }))
    return state === "open" ? open : [...open, ...closed]
  }, [d, snap.positions, state])
  const g = useSort(rows, {
    coin: r => r.symbol.toLowerCase(), invested: r => r.cost, worth: r => r.now, gain: r => r.now - r.cost,
    pct: r => (r.cost ? (r.now - r.cost) / r.cost : null), time: r => r.until ?? r.since,
  }, "live-positions")
  if (!d) return empty
  const sorted = g.sort ? g.sorted : [...rows].sort((a, b) => (b.until ?? Infinity) - (a.until ?? Infinity) || b.since - a.since)
  const px = d.sol_usd ?? snap.sol_usd
  const money = (v: number) => (px ? usd(v * px) : `${v.toFixed(3)} SOL`)
  const signed = (v: number) => `${v >= 0 ? "+" : "−"}${money(Math.abs(v))}`
  const cost = d.open.reduce((a, p) => a + p.cost_sol, 0)
  const worth = d.open.reduce((a, p) => a + p.value_sol, 0)
  const toggle = (k: string) => setOpenRows(cur => {
    const next = new Set(cur)
    if (next.has(k)) next.delete(k)
    else next.add(k)
    return next
  })
  return (
    <div className="space-y-4">
      <Banner d={d} ok={live.ok} />
      <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
        <Stat label="Coins held" value={d.open.length} sub="open positions in the live wallet" />
        <Stat label="Put in" value={money(cost)} sub="swap + fees + token account rent" />
        <Stat label="Worth now" value={money(worth)} tone={tone(worth - cost)}
              sub={<span className={tone(worth - cost)}>{signed(worth - cost)} ({cost ? pct((worth / cost - 1) * 100, 0, true) : "—"})</span>} />
        <Stat label="Cash" value={money(d.cash_sol)} sub="free to buy with" />
      </div>

      <CashChart d={d} px={px} />

      <Section title="Positions" description="One row per position; tap it for its chart and details. Now = what a real sell returns (re-checked every 20 s), or what it sold for.">
        <div className="mb-3 flex flex-wrap items-center gap-1.5">
          <Chip on={state === "open"} onClick={() => setState("open")}>Open</Chip>
          <Chip on={state === "all"} onClick={() => setState("all")}>Incl. closed</Chip>
        </div>
        {sorted.length === 0 ? (
          <Empty>{state === "open" ? <>Nothing open right now. Live positions usually last a few minutes: tap <b>Incl. closed</b> for the recent ones.</> : "No positions yet."}</Empty>
        ) : (
          <>
            <div className="md:hidden"><SortChips {...g} options={[["worth", "Worth"], ["gain", "Gain"], ["pct", "Gain %"], ["time", "Time"], ["coin", "Coin"]]} /></div>
            <Table>
              <TableHeader>
                <TableRow>
                  <SortHead k="coin" {...g}>Coin</SortHead>
                  <SortHead k="invested" right {...g} className="hidden sm:table-cell">Put in</SortHead>
                  <SortHead k="worth" right {...g}>Now</SortHead>
                  <SortHead k="gain" right {...g}>Gain</SortHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sorted.map(r => {
                  const expanded = openRows.has(r.key)
                  const gain = r.now - r.cost
                  const tn = tone(gain)
                  return (
                    <Fragment key={r.key}>
                      <TableRow className="cursor-pointer" onClick={() => toggle(r.key)}>
                        <TableCell className="font-medium">
                          <span className="flex items-center gap-1.5">
                            <ChevronRight className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform", expanded && "rotate-90")} />
                            <span onClick={e => e.stopPropagation()}>
                              <CoinLink mint={r.mint} className="underline decoration-muted-foreground/40 underline-offset-2 hover:decoration-foreground">{r.symbol || r.mint.slice(0, 6)}</CoinLink>
                            </span>
                            {r.status !== "open" && (
                              <Badge className={r.status === "stuck" ? "bg-negative/15 text-negative" : "bg-muted text-muted-foreground"}>{r.status}</Badge>
                            )}
                          </span>
                        </TableCell>
                        <TableCell className="num hidden text-right text-muted-foreground sm:table-cell">{money(r.cost)}</TableCell>
                        <TableCell className={cn("num text-right font-medium", tn)}>{money(r.now)}</TableCell>
                        <TableCell className={cn("num text-right", tn)}>
                          {signed(gain)} <span className="text-xs">({r.cost ? pct((gain / r.cost) * 100, 0, true) : "—"})</span>
                        </TableCell>
                      </TableRow>
                      {expanded && (
                        <TableRow className="bg-muted/25 hover:bg-muted/25">
                          <TableCell colSpan={4} className="whitespace-normal">
                            <PositionDetail r={r} px={px} now={d.now} />
                          </TableCell>
                        </TableRow>
                      )}
                    </Fragment>
                  )
                })}
              </TableBody>
            </Table>
          </>
        )}
      </Section>
    </div>
  )
}

/** The expanded row: the position's value over time against what it cost, and the details. */
function PositionDetail({ r, px, now }: { r: Row; px: number | null; now: number }) {
  const data = (r.path ?? []).map(([t, v]) => ({ t, v: px ? v * px : v }))
  const line = r.now >= r.cost ? "var(--positive)" : "var(--negative)"
  const span = data.length ? data[data.length - 1].t - data[0].t : 0
  return (
    <div className="space-y-2 py-1">
      <div className="h-32">
        {data.length < 2 ? <div className="flex h-full items-center justify-center text-xs text-muted-foreground">The chart fills in as the position is re-checked (positions bought before the charts existed have none).</div> : (
          <ChartContainer config={{ v: { label: "worth", color: line } } satisfies ChartConfig} className="aspect-auto h-32 w-full">
            <LineChart data={data} margin={{ left: 4, right: 8, top: 6, bottom: 0 }}>
              <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} tickFormatter={clockFor(span)} tickLine={false} axisLine={false} minTickGap={48} fontSize={10} />
              <YAxis domain={["auto", "auto"]} tickFormatter={v => (px ? usd(v) : num(v, 3))} tickLine={false} axisLine={false} width={48} fontSize={10} />
              <ReferenceLine y={px ? r.cost * px : r.cost} stroke="var(--muted-foreground)" strokeDasharray="3 3" />
              <ChartTooltip content={<ChartTooltipContent labelFormatter={(_, p) => clock(p?.[0]?.payload?.t ?? 0)} formatter={v => (px ? usd(Number(v)) : `${num(Number(v), 4)} SOL`)} />} />
              <Line dataKey="v" type="monotone" stroke={line} strokeWidth={2} dot={false} isAnimationActive={false} />
            </LineChart>
          </ChartContainer>
        )}
      </div>
      {r.open && <HoldingFacts o={r.open} px={px} />}
      <div className="num grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-4">
        <div><span className="text-muted-foreground">paper, same trade </span><span className={tone(r.paperPct)}>{r.paperPct != null ? pct(r.paperPct, 0, true) : "—"}</span></div>
        <div><span className="text-muted-foreground">{r.until ? "held " : "holding "}</span>{duration((r.until ?? now) - r.since)}</div>
        <div><span className="text-muted-foreground">price impact </span>{r.impact != null ? `${num(r.impact, 1)}%` : "—"}</div>
        <div><span className="text-muted-foreground">route </span>{r.route || "—"}</div>
        <div><span className="text-muted-foreground">bought </span>{dayClock(r.since)}</div>
        {r.until && <div><span className="text-muted-foreground">ended </span>{r.why} · {ago(r.until, now)}</div>}
        {r.fails > 0 && <div className="text-negative">{r.fails} failed sell tries</div>}
      </div>
    </div>
  )
}

/** A coin the live wallet holds: the buy, where it stands now, why it was bought, how it will be sold. */
function HoldingFacts({ o, px }: { o: DryRunView["open"][number]; px: number | null }) {
  const money = (s: number | null | undefined) => (s == null ? "—" : px ? usd(s * px) : `${num(s, 4)} SOL`)
  const gain = o.value_sol + (o.proceeds_sol ?? 0) - o.cost_sol
  const peak = o.peak_value_sol != null ? (o.peak_value_sol + (o.proceeds_sol ?? 0)) / o.cost_sol - 1 : null
  const mx = o.mcap_usd && o.entry_mcap_usd ? o.mcap_usd / o.entry_mcap_usd : null
  const c = o.council
  return (
    <div className="num grid gap-2 text-xs sm:grid-cols-2">
      <div className="rounded-md bg-muted/40 p-2">
        <div className="text-muted-foreground">Initial buy</div>
        <div className="mt-0.5">{money(o.sol_in)} at {o.entry_mcap_usd ? `${usd(o.entry_mcap_usd)} mcap` : "—"} · {dayClock(o.entry_ts)}</div>
        <div className="text-muted-foreground">cost incl. fees + token account rent {money(o.cost_sol)}{o.sold_pct ? ` · ${num(o.sold_pct, 0)}% already sold for ${money(o.proceeds_sol)}` : ""}</div>
      </div>
      <div className="rounded-md bg-muted/40 p-2">
        <div className="text-muted-foreground">Now</div>
        <div className={cn("mt-0.5", tone(gain))}>{money(o.value_sol)} held · {gain >= 0 ? "+" : "−"}{money(Math.abs(gain))} ({pct(100 * gain / o.cost_sol, 0, true)})</div>
        <div className="text-muted-foreground">mcap {o.mcap_usd ? usd(o.mcap_usd) : "—"}{mx ? ` (${num(mx, 2)}x since the buy)` : ""} · best so far {peak != null ? pct(100 * peak, 0, true) : "—"}</div>
      </div>
      <div className="rounded-md bg-muted/40 p-2">
        <div className="text-muted-foreground">Why it was bought</div>
        <div className="mt-0.5 break-words">{o.why ? o.why.split(",").join(" · ") : "—"}</div>
      </div>
      <div className="rounded-md bg-muted/40 p-2">
        <div className="text-muted-foreground">How it will be sold</div>
        <div className="mt-0.5">{o.exit_plan?.length ? o.exit_plan.join(" · ") : "—"}</div>
        <div className="mt-1 text-muted-foreground">Council: {c ? <span className={c.verdict === "sell" ? "text-negative" : "text-positive"}>{c.verdict === "sell" ? "sell" : "hold"}{c.agreement != null ? ` (${Math.round(c.agreement * 100)}% agree)` : ""}</span> : "not reviewed yet"}</div>
        {c?.why?.length ? <div className="mt-0.5 text-muted-foreground">{c.why.join(" · ")}</div> : null}
      </div>
    </div>
  )
}

function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button type="button" onClick={onClick}
      className={cn("h-7 rounded-full border px-2.5 text-xs transition-colors", on ? "border-foreground/30 bg-foreground text-background" : "hover:bg-muted")}>
      {children}
    </button>
  )
}

/** The wallet split into cash and what is in positions, over time. */
function CashChart({ d, px }: { d: Started; px: number | null }) {
  const data = useMemo(() => d.series.map(pt => {
    const [t, e] = pt
    const cash = pt.length > 2 ? (pt as [number, number, number])[2] : e
    const k = px ?? 1
    return { t, cash: cash * k, positions: Math.max(0, e - cash) * k }
  }), [d.series, px])
  const span = data.length ? data[data.length - 1].t - data[0].t : 0
  const config = { cash: { label: "Cash", color: "var(--chart-2)" }, positions: { label: "In positions", color: "var(--positive)" } } satisfies ChartConfig
  return (
    <Section title="Cash and positions" description="The live wallet over time: cash, plus what open positions would sell for. Each buy moves money from cash into positions.">
      {data.length < 2 ? <Empty>Collecting points: one a minute, plus one at every trade.</Empty> : (
        <ChartContainer config={config} className="aspect-auto h-56 w-full">
          <AreaChart data={data} margin={{ left: 4, right: 8, top: 8 }}>
            <CartesianGrid vertical={false} />
            <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} tickFormatter={clockFor(span)} tickLine={false} axisLine={false} minTickGap={56} />
            <YAxis tickFormatter={v => (px ? usd(v) : num(v, 3))} tickLine={false} axisLine={false} width={56} />
            <ChartTooltip content={<ChartTooltipContent labelFormatter={(_, p) => clock(p?.[0]?.payload?.t ?? 0)} formatter={(v, name) => `${name === "cash" ? "cash" : "in positions"} ${px ? usd(Number(v)) : num(Number(v), 4)}`} />} />
            <Area dataKey="cash" stackId="w" type="stepAfter" stroke="var(--chart-2)" fill="var(--chart-2)" fillOpacity={0.25} isAnimationActive={false} />
            <Area dataKey="positions" stackId="w" type="stepAfter" stroke="var(--positive)" fill="var(--positive)" fillOpacity={0.35} isAnimationActive={false} />
          </AreaChart>
        </ChartContainer>
      )}
    </Section>
  )
}

function PositionsTable({ d, px, snap, limit }: { d: Started; px: number | null; snap: Snapshot; limit?: number }) {
  const inspect = useInspect()
  const paper = new Map(snap.positions.filter(p => p.role === "champion").map(p => [p.mint, p]))
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[38rem] text-sm">
        <thead className="text-xs text-muted-foreground">
          <tr className="text-left"><th className="py-1 pr-3 font-normal">coin</th><th className="py-1 pr-3 font-normal">put in</th>
            <th className="py-1 pr-3 font-normal">worth now</th><th className="py-1 pr-3 font-normal">paper</th>
            <th className="py-1 pr-3 font-normal">bought</th><th className="py-1 pr-3 font-normal">impact</th><th className="py-1 font-normal">route</th></tr>
        </thead>
        <tbody className="num">
          {d.open.slice(0, limit ?? d.open.length).map(p => {
            const x = p.cost_sol ? p.value_sol / p.cost_sol - 1 : null
            const pp = paper.get(p.mint)
            const px2 = pp && pp.invested_sol ? pp.worth_sol / pp.invested_sol - 1 : null
            return (
              <tr key={p.mint} className="border-t">
                <td className="py-1.5 pr-3 font-medium">
                  <button type="button" onClick={() => inspect.coin(p.mint)} className="hover:underline">{p.symbol || p.mint.slice(0, 6)}</button>
                </td>
                <td className="py-1.5 pr-3">{px ? usd(p.cost_sol * px) : sol(p.cost_sol, 4)}</td>
                <td className={cn("py-1.5 pr-3", tone(x))}>{px ? usd(p.value_sol * px) : sol(p.value_sol, 4)} ({pct(x != null ? x * 100 : null, 0, true)})
                  {p.sell_fails > 0 && <span className="ml-1 text-xs text-negative">· {p.sell_fails} failed sells</span>}</td>
                <td className={cn("py-1.5 pr-3 text-xs", tone(px2))}>{px2 != null ? pct(px2 * 100, 0, true) : <span className="text-muted-foreground">sold</span>}</td>
                <td className="py-1.5 pr-3 text-muted-foreground" title={dayClock(p.entry_ts)}>{ago(p.entry_ts, d.now)}</td>
                <td className="py-1.5 pr-3 text-muted-foreground">{num(p.impact_pct, 1)}%</td>
                <td className="py-1.5 text-muted-foreground">{p.route || "—"}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

// ── trades ───────────────────────────────────────────────────────────────────
export function LiveTradesPage({ snap, live }: { snap: Snapshot; live: Poll }) {
  const { d, empty } = useStarted(live)
  if (!d) return empty
  const px = d.sol_usd ?? snap.sol_usd
  const n = (side: LiveFill["side"]) => d.tape.filter(t => t.side === side).length
  return (
    <div className="space-y-4">
      <Banner d={d} ok={live.ok} />
      <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
        <Tile label="Bought" value={String(n("buy"))} sub="real swaps that would have landed" />
        <Tile label="Sold" value={String(n("sell"))} sub={`${d.won} of ${d.trades} closed trades won`} />
        <Tile label="Not bought" value={String(n("missed"))} sub={`${d.counts.no_route} no route · ${d.counts.slippage} price ran past the limit`} />
        <Tile label="Sell trouble" value={String(n("sell_failed") + n("stuck"))} t={n("stuck") ? "text-negative" : undefined}
              sub={`${n("sell_failed")} failed tries · ${n("stuck")} never sold`} />
      </div>
      <Section title="Every fill" description="Each live buy and sell as it happened, with the real price against the price paper got. Newest first.">
        {d.tape.length === 0 ? <Empty>No trades yet. The first one comes with the champions' next buy.</Empty> : (
          <ol className="divide-y">{d.tape.map((t, i) => <FillRow key={`${t.ts}-${i}`} t={t} px={px} now={d.now} />)}</ol>
        )}
      </Section>
      <Section title="Closed trades: live vs paper" description="The whole trade, start to finish, next to what the paper wallet booked for the same trade.">
        {d.history.length === 0 ? <Empty>No closed trades yet.</Empty> : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[44rem] text-sm">
              <thead className="text-xs text-muted-foreground">
                <tr className="text-left"><th className="py-1 pr-3 font-normal">coin</th><th className="py-1 pr-3 font-normal">closed</th>
                  <th className="py-1 pr-3 text-right font-normal">live</th><th className="py-1 pr-3 text-right font-normal">paper</th>
                  <th className="py-1 pr-3 text-right font-normal">entry vs paper</th><th className="py-1 pr-3 text-right font-normal">impact in / out</th>
                  <th className="py-1 font-normal">why it ended</th></tr>
              </thead>
              <tbody className="num">{d.history.map((t, i) => <TradeRow key={`${t.mint}-${t.exit_ts}-${i}`} t={t} px={px} now={d.now} />)}</tbody>
            </table>
          </div>
        )}
      </Section>
    </div>
  )
}

const FILL: Record<LiveFill["side"], { label: string; icon: ComponentType<{ className?: string }>; tone: string }> = {
  buy: { label: "bought", icon: ArrowUpRight, tone: "bg-chart-2/15 text-chart-2" },
  sell: { label: "sold", icon: ArrowDownRight, tone: "bg-positive/15 text-positive" },
  missed: { label: "not bought", icon: XCircle, tone: "bg-muted text-muted-foreground" },
  sell_failed: { label: "sell failed", icon: TriangleAlert, tone: "bg-warning/15 text-warning" },
  stuck: { label: "couldn't sell", icon: TriangleAlert, tone: "bg-negative/15 text-negative" },
  stopped: { label: "stopped at the loss limit", icon: ShieldCheck, tone: "bg-negative/15 text-negative" },
  blocked: { label: "Overseer stopped the buy", icon: ShieldCheck, tone: "bg-negative/15 text-negative" },
}
const OTHER = { label: "event", icon: CircleDashed, tone: "bg-muted text-muted-foreground" }

function FillRow({ t, px, now, compact }: { t: LiveFill; px: number | null; now: number; compact?: boolean }) {
  const f = FILL[t.side] ?? OTHER                     // a kind this page does not know yet must not blank it
  const I = f.icon
  const gap = t.price && t.paper_price ? 100 * (t.price / t.paper_price - 1) : null
  const sold = t.side === "sell"
  const label = sold && t.partial ? `took profit (${Math.round((t.frac ?? 0) * 100)}%)` : f.label
  // for a buy, a higher real price is worse; for a sell, a lower one is
  const gapTone = gap == null ? "" : tone(sold ? gap : -gap)
  return (
    <li className="flex gap-3 py-2">
      <span className={cn("mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full", f.tone)}><I className="size-3.5" /></span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
          <span className="font-medium">{label}</span>
          {(t.symbol || t.mint) && <span className="font-semibold">{t.symbol || t.mint.slice(0, 6)}</span>}
          {t.sol != null && t.sol > 0 && <span className="num text-muted-foreground">{px ? usd(t.sol * px) : sol(t.sol, 4)}</span>}
          {sold && t.pnl_sol != null && (
            <span className={cn("num", tone(t.pnl_sol))}>{px ? usd(t.pnl_sol * px) : sol(t.pnl_sol, 4)}
              {t.paper_pnl_pct != null && <span className="text-xs text-muted-foreground"> · paper {pct(t.paper_pnl_pct, 0, true)}</span>}</span>
          )}
          <span className="ml-auto text-xs text-muted-foreground" title={dayClock(t.ts)}>{ago(t.ts, now)}</span>
        </div>
        <div className="num mt-0.5 flex flex-wrap gap-x-3 text-xs text-muted-foreground">
          {gap != null && <span>price vs paper <span className={gapTone}>{num(gap, 1, true)}%</span></span>}
          {t.impact_pct != null && <span>impact {num(t.impact_pct, 1)}%</span>}
          {!!t.whales && <span className="text-chart-2">{t.whales} whale{t.whales === 1 ? "" : "s"} in</span>}
          {t.size_mult != null && t.size_mult > 1 && <span className="text-chart-2">{num(t.size_mult, 2)}x bet</span>}
          {sold && t.x != null && <span>{num(t.x, 2)}x</span>}
          {!compact && t.route && <span>{t.route}</span>}
          {t.why && <span className="[overflow-wrap:anywhere]">{t.why}</span>}
          {!compact && <span className="font-mono">{t.key}</span>}
        </div>
      </div>
    </li>
  )
}

function TradeRow({ t, px, now }: { t: DryRunTrade; px: number | null; now: number }) {
  const entryGap = t.entry_price && t.paper_entry_price ? 100 * (t.entry_price / t.paper_entry_price - 1) : null
  return (
    <tr className="border-t">
      <td className="py-1.5 pr-3 font-medium">{t.symbol || t.mint.slice(0, 6)}</td>
      <td className="py-1.5 pr-3 text-muted-foreground" title={dayClock(t.exit_ts)}>{ago(t.exit_ts, now)}</td>
      <td className={cn("py-1.5 pr-3 text-right", tone(t.pnl_sol))}>{pct(t.pnl_pct, 0, true)}{px ? ` (${usd(t.pnl_sol * px)})` : ""}</td>
      <td className={cn("py-1.5 pr-3 text-right", tone(t.paper_pnl_pct))}>{t.paper_pnl_pct != null ? pct(t.paper_pnl_pct, 0, true) : "—"}</td>
      <td className={cn("py-1.5 pr-3 text-right", tone(entryGap != null ? -entryGap : null))}>{entryGap != null ? `${num(entryGap, 1, true)}%` : "—"}</td>
      <td className="py-1.5 pr-3 text-right text-muted-foreground">{t.buy_impact_pct != null ? num(t.buy_impact_pct, 1) : "—"} / {t.sell_impact_pct != null ? num(t.sell_impact_pct, 1) : "—"}%</td>
      <td className="py-1.5 text-muted-foreground" title={t.note}>
        {t.status === "stuck" ? <Badge className="bg-negative/15 text-negative">couldn't sell</Badge> : t.exit_reason}
      </td>
    </tr>
  )
}

// ── strategy ─────────────────────────────────────────────────────────────────
export function LiveStrategyPage({ snap, live, swarm }: { snap: Snapshot; live: Poll; swarm: Swarm | null }) {
  const inspect = useInspect()
  const { d } = useStarted(live)
  const byKey = useMemo(() => {
    const m = new Map<string, { n: number; won: number; pnl: number; paper: number[] }>()
    for (const t of d?.history ?? []) {
      const r = m.get(t.key) ?? { n: 0, won: 0, pnl: 0, paper: [] }
      r.n += 1; r.won += t.pnl_sol > 0 ? 1 : 0; r.pnl += t.pnl_sol
      if (t.paper_pnl_pct != null) r.paper.push(t.paper_pnl_pct)
      m.set(t.key, r)
    }
    return m
  }, [d])
  const champions = snap.strategies.filter(s => s.role === "champion")   // the ones that actually trade first
    .sort((a, b) => ((byKey.get(b.key)?.n ?? 0) + b.closed + b.open) - ((byKey.get(a.key)?.n ?? 0) + a.closed + a.open))
  const px = d?.sol_usd ?? snap.sol_usd
  const fresh = (swarm?.agents ?? []).filter(a => a.status === "swarm").sort((a, b) => (b.fitness ?? -1e9) - (a.fitness ?? -1e9)).slice(0, 3)
  return (
    <div className="space-y-4">
      {d && <Banner d={d} ok={live.ok} />}
      <Section title="What the live wallet copies" description="The swarm and the council evolve strategies on paper. The best one is promoted to champion, and the live wallet copies every champion trade at real prices.">
        {champions.length === 0 ? <Empty>No champion right now.</Empty> : (
          <ul className="space-y-3">
            {champions.map(s => {
              const r = byKey.get(s.key)
              return (
                <li key={s.key} className="rounded-lg border p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <RoleBadge role="champion" />
                    <StrategyName k={s.key} />
                    <button type="button" onClick={() => inspect.agent(s.key)} className="ml-auto rounded-md border px-2 py-1 text-xs hover:bg-muted">rules, family, why it wins</button>
                  </div>
                  <div className="num mt-2 grid gap-2 text-xs sm:grid-cols-3">
                    <div className="rounded-md bg-muted/40 p-2"><div className="text-muted-foreground">Live (real prices)</div>
                      {r ? <div className={cn("mt-0.5 text-sm", tone(r.pnl))}>{px ? usd(r.pnl * px) : sol(r.pnl, 4)} · {r.won}/{r.n} won</div> : <div className="mt-0.5">no live trades yet</div>}</div>
                    <div className="rounded-md bg-muted/40 p-2"><div className="text-muted-foreground">Paper, same trades</div>
                      <div className="mt-0.5 text-sm">{r?.paper.length ? `${pct(r.paper.reduce((a, b) => a + b, 0) / r.paper.length, 1, true)} a trade` : "—"}</div></div>
                    <div className="rounded-md bg-muted/40 p-2"><div className="text-muted-foreground">Paper this session</div>
                      <div className={cn("mt-0.5 text-sm", tone(s.pnl_sol))}>{s.closed} closed · {pct(s.win_pct, 0)} won · {sol(s.pnl_sol, 3)} SOL</div></div>
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </Section>
      {fresh.length > 0 && (
        <Section title="Next in line" description="The fittest swarm agents on paper. One of them becomes champion (and so what live copies) when it beats the current one.">
          <ul className="divide-y">
            {fresh.map(a => (
              <li key={a.key} className="flex flex-wrap items-center gap-2 py-2 text-sm">
                <StrategyName k={a.key} />
                <span className="num text-xs text-muted-foreground">fitness {num(a.fitness, 1, true)} · {a.live_n} paper trades</span>
                <button type="button" onClick={() => inspect.agent(a.key)} className="ml-auto rounded-md border px-2 py-1 text-xs hover:bg-muted">details</button>
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  )
}

// ── council ──────────────────────────────────────────────────────────────────
export function LiveCouncilPage({ snap, swarm }: { snap: Snapshot; swarm: Swarm | null }) {
  return (
    <div className="space-y-4">
      <div className="rounded-xl border bg-muted/30 px-4 py-2.5 text-sm text-muted-foreground">
        One council for both modes. It studies why coins made money, proposes entry rules, and votes; the swarm breeds them into new strategies on paper. Whatever becomes champion is what the live wallet trades.
      </div>
      {!swarm ? <Empty>Loading the council…</Empty> : swarm.error ? <Empty>Council data unavailable: {swarm.error}</Empty> : <CouncilView swarm={swarm} snap={snap} />}
    </div>
  )
}

// ── right now ────────────────────────────────────────────────────────────────
const DOING: Record<string, string> = {
  bought: "bg-chart-1/15 text-chart-1", holding: "bg-chart-1/15 text-chart-1", sold: "bg-chart-3/15 text-chart-3",
  checking: "bg-chart-2/15 text-chart-2", watching: "bg-muted text-muted-foreground", skipped: "bg-muted text-muted-foreground/70",
}

/** What the shared brain is doing at this moment: live only acts when a champion buys, so show the work behind it. */
function RightNow({ snap, d }: { snap: Snapshot; d: Started }) {
  const liveMints = new Set(d.open.map(o => o.mint))
  const missed = new Map<string, string>()
  for (const t of d.tape) if (t.side === "missed" && !missed.has(t.mint)) missed.set(t.mint, (t.why ?? "").replace(/: real price/, ": real price"))
  const champs = snap.strategies.filter(s => s.role === "champion")
  const clk = snap.swarm_clock
  const council = !clk ? "swarm off"
    : clk.running ? `generation running${clk.phase ? `: ${clk.phase}` : ""}${clk.started_ts ? ` (${duration(Date.now() / 1000 - clk.started_ts)})` : ""}`
    : clk.next_ts ? `next generation in ${duration(Math.max(0, clk.next_ts - Date.now() / 1000))}` : "waiting"
  const coins = snap.tokens.filter(t => t.phase !== "watching").slice(0, 6)
  const champMints = new Set(snap.positions.filter(p => p.role === "champion").map(p => p.mint))
  const counts = snap.tokens.reduce<Record<string, number>>((a, t) => ({ ...a, [t.phase]: (a[t.phase] ?? 0) + 1 }), {})
  return (
    <Section title="Right now" description={`What the whole bot is doing. The live wallet holds ${liveMints.size === 0 ? "nothing right now" : `${liveMints.size} coin(s)`}: see Positions. "Paper champion" coins are held by the champions on paper; live copies their buys at real prices but can miss them. "Paper agent" coins belong to swarm and trial strategies that live never copies.`}
             action={<MoreLink page="live-scanner" />}>
      <div className="num grid gap-2 text-xs sm:grid-cols-3">
        <div className="rounded-md bg-muted/40 p-2"><div className="text-muted-foreground">Champions live copies</div>
          <div className="mt-0.5 text-sm">{champs.length ? champs.map(c => c.key.split("@")[0]).join(", ") : "none"}</div></div>
        <div className="rounded-md bg-muted/40 p-2"><div className="text-muted-foreground">Coins on the board</div>
          <div className="mt-0.5 text-sm">{counts.checking ?? 0} checking · {counts.watching ?? 0} watching · {counts.skipped ?? 0} skipped</div></div>
        <div className="rounded-md bg-muted/40 p-2"><div className="text-muted-foreground">Swarm and council</div>
          <div className="mt-0.5 text-sm">{council}</div></div>
      </div>
      {coins.length === 0 ? <div className="mt-3"><Empty>Nothing being checked this second.</Empty></div> : (
        <ul className="mt-3 divide-y">
          {coins.map(t => (
            <li key={t.mint + t.ts} className="flex items-center gap-2 py-1.5 text-sm">
              <CoinLink mint={t.mint}>{t.symbol}</CoinLink>
              <Badge className={DOING[t.phase] ?? "bg-muted text-muted-foreground"}>{t.phase}</Badge>
              {t.phase === "holding" && (liveMints.has(t.mint)
                ? <Badge className="bg-positive/15 text-positive">live wallet</Badge>
                : <Badge className={champMints.has(t.mint) ? "bg-chart-1/15 text-chart-1" : "bg-muted text-muted-foreground"}>
                    {champMints.has(t.mint) ? "paper champion" : "paper agent"}</Badge>)}
              {t.phase === "holding" && champMints.has(t.mint) && !liveMints.has(t.mint) && missed.has(t.mint) &&
                <span className="truncate text-xs text-negative">live missed it: {missed.get(t.mint)}</span>}
              <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">{t.detail}</span>
              <span className="num shrink-0 text-xs text-muted-foreground">{t.age}</span>
            </li>
          ))}
        </ul>
      )}
    </Section>
  )
}

// ── bits ─────────────────────────────────────────────────────────────────────
function Tile({ label, value, sub, t }: { label: string; value: string; sub: ReactNode; t?: string }) {
  return (
    <div className="rounded-xl border bg-card p-3 sm:p-4">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={cn("num mt-1 text-xl font-semibold sm:text-2xl", t)}>{value}</div>
      <div className="mt-1 text-xs text-muted-foreground">{sub}</div>
    </div>
  )
}

function LiveChart({ d, px, className }: { d: Started; px: number | null; className?: string }) {
  const data = useMemo(() => d.series.map(pt => ({ t: pt[0], usd: px ? pt[1] * px : pt[1] })), [d.series, px])
  const config = { usd: { label: "Live wallet", color: "var(--chart-1)" } } satisfies ChartConfig
  const down = (d.pnl_pct ?? 0) < 0
  const span = data.length ? data[data.length - 1].t - data[0].t : 0
  const start = px ? d.start_sol * px : d.start_sol
  return (
    <Section className={className} title="Live wallet" description={`Cash plus open positions at real sell prices, since ${dayClock(d.started_ts)}. Dashed line: the ${usd(d.start_usd, 0)} start.`}>
      {data.length < 2 ? <Empty>Collecting points: one a minute, plus one at every trade.</Empty> : (
        <ChartContainer config={config} className="aspect-auto h-64 w-full">
          <AreaChart data={data} margin={{ left: 4, right: 8, top: 8 }}>
            <defs>
              <linearGradient id="liveFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor={down ? "var(--negative)" : "var(--positive)"} stopOpacity={0.35} />
                <stop offset="95%" stopColor={down ? "var(--negative)" : "var(--positive)"} stopOpacity={0.02} />
              </linearGradient>
            </defs>
            <CartesianGrid vertical={false} />
            <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} tickFormatter={clockFor(span)} tickLine={false} axisLine={false} minTickGap={56} />
            <YAxis tickFormatter={v => (px ? usd(v) : num(v, 3))} tickLine={false} axisLine={false} width={56} domain={["auto", "auto"]} />
            <ReferenceLine y={start} stroke="var(--muted-foreground)" strokeDasharray="4 4" />
            <ChartTooltip content={<ChartTooltipContent labelFormatter={(_, p) => clock(p?.[0]?.payload?.t ?? 0)} formatter={v => (px ? usd(Number(v)) : `${num(Number(v), 4)} SOL`)} />} />
            <Area dataKey="usd" type="monotone" stroke={down ? "var(--negative)" : "var(--positive)"} strokeWidth={2} fill="url(#liveFill)" isAnimationActive={false} />
          </AreaChart>
        </ChartContainer>
      )}
    </Section>
  )
}

// ── whales ───────────────────────────────────────────────────────────────────
const VERDICT: Record<string, string> = {
  trust: "bg-positive/15 text-positive", distrust: "bg-negative/15 text-negative", unsure: "bg-chart-2/15 text-chart-2",
}

export function LiveWhalesPage({ snap, live }: { snap: Snapshot; live: Poll }) {
  const { d } = useStarted(live)
  const whales = (snap.whales ?? []).filter(w => w.status === "whale")
  const trusted = whales.filter(w => w.council === "trust").length
  const distrusted = whales.filter(w => w.council === "distrust").length
  // live trades with whales in vs without (paired by coin with the closed-trade record)
  const buys = new Map((d?.tape ?? []).filter(t => t.side === "buy").map(t => [t.mint, t]))
  const closed = d?.history ?? []
  const withW = closed.filter(t => (buys.get(t.mint)?.whales ?? 0) > 0)
  const without = closed.filter(t => !((buys.get(t.mint)?.whales ?? 0) > 0))
  const avg = (xs: typeof closed) => (xs.length ? xs.reduce((a, t) => a + (t.pnl_pct ?? 0), 0) / xs.length : null)
  return (
    <div className="space-y-4">
      <WhaleTape />
      {d && <Banner d={d} ok={live.ok} />}
      <div className="rounded-xl border bg-muted/30 px-4 py-2.5 text-sm text-muted-foreground">
        Whales are wallets that keep buying coins early that later ran 3x, and exit well. Each generation the council judges
        the top ones: trusted whales are polled live first, distrusted ones stop counting. A strategy can carry a bet-boost
        gene (bet more when trusted whales bought in the last 10 minutes); the live wallet copies that bigger bet too.
      </div>
      <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
        <Tile label="Whales" value={String(whales.length)} sub={`${whales.filter(w => w.tracked).length} polled live`} />
        <Tile label="Council verdicts" value={`${trusted} trusted`} sub={`${distrusted} distrusted · ${whales.length - trusted - distrusted} not judged / unsure`} />
        <Tile label="Live trades with whales in" value={String(withW.length)} t={tone(avg(withW))}
              sub={withW.length ? <>avg {pct(avg(withW), 0, true)} a trade</> : "none closed yet"} />
        <Tile label="Live trades without" value={String(without.length)} t={tone(avg(without))}
              sub={without.length ? <>avg {pct(avg(without), 0, true)} a trade</> : "none closed yet"} />
      </div>
      <Section title="The whale book" description="Sorted by the council's verdict, then score. Hover a verdict for the judges' reasons.">
        {whales.length === 0 ? <Empty>No whales yet: the first scan runs with the next swarm generation.</Empty> : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[44rem] text-sm">
              <thead className="text-xs text-muted-foreground">
                <tr className="text-left"><th className="py-1 pr-3 font-normal">wallet</th><th className="py-1 pr-3 font-normal">council</th>
                  <th className="py-1 pr-3 text-right font-normal">score</th><th className="py-1 pr-3 text-right font-normal">win</th>
                  <th className="py-1 pr-3 text-right font-normal">median exit</th><th className="py-1 pr-3 text-right font-normal">early on 3x coins</th>
                  <th className="py-1 font-normal">notes</th></tr>
              </thead>
              <tbody className="num">
                {[...whales].sort((a, b) => (["trust", "unsure", undefined, null, "distrust"].indexOf(a.council ?? undefined) -
                  ["trust", "unsure", undefined, null, "distrust"].indexOf(b.council ?? undefined)) || b.score - a.score).map(w => (
                  <tr key={w.address} className="border-t">
                    <td className="py-1.5 pr-3 font-mono text-xs" title={w.address}>{w.address.slice(0, 4)}…{w.address.slice(-4)}
                      {w.label && <span className="ml-1.5 font-sans text-muted-foreground">{w.label}</span>}</td>
                    <td className="py-1.5 pr-3" title={w.council_reason || "not judged yet"}>
                      {w.council ? <Badge className={VERDICT[w.council]}>{w.council}{w.council_score != null ? ` ${Math.round(w.council_score * 100)}%` : ""}</Badge>
                        : <span className="text-xs text-muted-foreground">not judged</span>}
                    </td>
                    <td className="py-1.5 pr-3 text-right">{num(w.score, 2)}</td>
                    <td className="py-1.5 pr-3 text-right">{w.win != null ? pct(w.win * 100, 0) : "—"}</td>
                    <td className="py-1.5 pr-3 text-right">{w.median_exit != null ? `${num(w.median_exit, 2)}x` : "—"}</td>
                    <td className="py-1.5 pr-3 text-right">{w.early_good}</td>
                    <td className="py-1.5 text-xs text-muted-foreground">
                      {w.tracked && <span className="mr-2 text-chart-2">polled live</span>}
                      {w.flag && <span className="text-negative">{w.flag}</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </div>
  )
}

// ── attempts and what live teaches paper ─────────────────────────────────────
function NewAttempt({ next, className }: { next: number; className?: string }) {
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState("")
  const go = async () => {
    if (!window.confirm(`End this attempt and start attempt #${next} with a fresh $50? Open positions are sold at their real value.`)) return
    setBusy(true)
    try {
      const r = await fetch("api/live/new-attempt", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true, why: "started from the dashboard" }) })
      setMsg(r.ok ? `Attempt #${next} starts within a few seconds.` : "The bot refused; try again.")
    } catch {
      setMsg("Can't reach the bot.")
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className={cn("flex flex-wrap items-center gap-2", className)}>
      <button type="button" onClick={go} disabled={busy}
        className="rounded-md border border-foreground/30 bg-foreground px-3 py-1.5 text-xs font-medium text-background disabled:opacity-60">
        Start attempt #{next} with a fresh $50
      </button>
      {msg && <span className="text-xs text-muted-foreground">{msg}</span>}
    </div>
  )
}

function LearningLine({ d }: { d: Started }) {
  const c = d.calibration
  if (!c) return null
  const share = Math.round(c.weight * 100)
  return (
    <div className="rounded-xl border bg-muted/30 px-4 py-2.5 text-xs text-muted-foreground">
      <span className="font-medium text-foreground">Paper learns from live:</span> {c.n} real-price buy attempts measured, so {share}% of
      paper's fills now use them
      {c.buy_gap_median != null && <> (real entries {pct(c.buy_gap_median * 100, 0, true)} vs paper</>}
      {c.sell_gap_median != null && <>, exits {pct(c.sell_gap_median * 100, 0, true)}</>}
      {c.buy_gap_median != null && <>)</>}
      {c.no_route > 0 && <>, {pct(c.no_route * 100, 0)} with no route</>}. The council also sees which live entries failed. The share grows as live
      measures more.
    </div>
  )
}

function AttemptsHistory({ d }: { d: Started }) {
  if (!d.attempts?.length) return (
    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
      <span>Attempt #{d.attempt}: stops itself at a {d.loss_limit_pct}% loss, like a real wallet's kill switch.</span>
      {!d.stopped && <NewAttempt next={d.attempt + 1} />}
    </div>
  )
  return (
    <Section title="Attempts" description={`Each attempt is a fresh $${d.start_usd} wallet; it stops itself at a ${d.loss_limit_pct}% loss. Newest first.`}
      action={!d.stopped ? <NewAttempt next={d.attempt + 1} /> : undefined}>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[34rem] text-sm">
          <thead className="text-xs text-muted-foreground">
            <tr className="text-left"><th className="py-1 pr-3 font-normal">#</th><th className="py-1 pr-3 font-normal">ran</th>
              <th className="py-1 pr-3 text-right font-normal">ended at</th><th className="py-1 pr-3 text-right font-normal">live</th>
              <th className="py-1 pr-3 text-right font-normal">paper</th><th className="py-1 pr-3 text-right font-normal">trades</th>
              <th className="py-1 font-normal">ended by</th></tr>
          </thead>
          <tbody className="num">
            {d.attempts.map(a => (
              <tr key={a.attempt} className="border-t">
                <td className="py-1.5 pr-3 font-medium">{a.attempt}</td>
                <td className="py-1.5 pr-3 text-muted-foreground">{dayClock(a.started_ts)} · {duration(a.ended_ts - a.started_ts)}</td>
                <td className="py-1.5 pr-3 text-right">{usd(a.end_usd)}</td>
                <td className={cn("py-1.5 pr-3 text-right", tone(a.pnl_pct))}>{pct(a.pnl_pct, 0, true)}</td>
                <td className={cn("py-1.5 pr-3 text-right", tone(a.paper_pnl_pct))}>{pct(a.paper_pnl_pct, 0, true)}</td>
                <td className="py-1.5 pr-3 text-right">{a.won}/{a.trades}</td>
                <td className="py-1.5 text-xs text-muted-foreground">{a.ended_by}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  )
}
