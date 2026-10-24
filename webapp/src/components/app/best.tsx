import { useEffect, useState } from "react"
import { CartesianGrid, Line, LineChart, ReferenceLine, XAxis, YAxis } from "recharts"
import { GitBranch, Trophy } from "lucide-react"

import { Empty, RoleBadge, SortHead, StrategyName, useSort } from "@/components/app/bits"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { ChartContainer, ChartTooltip, type ChartConfig } from "@/components/ui/chart"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import type { AgentProfile, BestEntry } from "@/lib/api"
import { useInspect } from "@/lib/inspect"
import { dayClock, duration, opLabel, pct, signedSol, signedUsd, splitKey, tone } from "@/lib/format"
import { cn } from "@/lib/utils"

/** Small summary box: the strategy that grew its wallet the most. Tap for its full profile. */
export function BestStat({ best }: { best: BestEntry[] | undefined }) {
  const inspect = useInspect()
  const top = best?.[0]
  const [id, v] = top ? splitKey(top.key) : ["", ""]
  return (
    <button type="button" disabled={!top} onClick={() => top && inspect.agent(top.key)} className="min-w-0 text-left">
        <Card size="sm" className="h-full transition-colors hover:bg-muted/40">
          <CardHeader>
            <CardDescription className="flex items-center gap-1 text-xs font-medium uppercase tracking-wide">
              <Trophy className="size-3 text-chart-3" />Best performing
            </CardDescription>
            <CardTitle className={cn("num truncate text-xl font-semibold sm:text-2xl", top ? tone(top.equity - 1) : "")}>
              {top ? pct(top.equity * 100, 0) : "—"}
            </CardTitle>
          </CardHeader>
          <CardContent className="min-w-0 space-y-0.5 text-xs text-muted-foreground">
            {top ? (
              <>
                <div className="truncate"><span className="text-foreground">{id}</span> <span className="font-mono text-[11px]">{v}</span></div>
                <div className="truncate">{top.top_token ? `best: ${top.top_token.symbol}` : `${top.trades} trades`} · tap for details</div>
              </>
            ) : <div>needs 5+ closed trades</div>}
          </CardContent>
        </Card>
    </button>
  )
}

export function ProfileSheet({ agentKey, onClose, onOpen }: { agentKey: string | null; onClose: () => void; onOpen: (k: string) => void }) {
  const [data, setData] = useState<AgentProfile | null>(null)
  useEffect(() => {
    if (!agentKey) return
    let alive = true
    setData(null)
    const load = () => fetch(`api/agent?key=${encodeURIComponent(agentKey)}`, { cache: "no-store" })
      .then(r => r.json()).then(d => { if (alive) setData(d) }).catch(() => {})
    load()
    const id = setInterval(load, 30000)
    return () => { alive = false; clearInterval(id) }
  }, [agentKey])
  return (
    <Sheet open={agentKey != null} onOpenChange={o => { if (!o) onClose() }}>
      <SheetContent className="overflow-y-auto data-[side=right]:w-full data-[side=right]:sm:max-w-3xl">
        {!data ? <div className="p-6"><Empty>Loading…</Empty></div> : data.error ? <div className="p-6"><Empty>{data.error}</Empty></div> : (
          <Profile d={data} onOpen={onOpen} />
        )}
      </SheetContent>
    </Sheet>
  )
}

function Profile({ d, onOpen }: { d: AgentProfile; onOpen: (k: string) => void }) {
  const [sid] = splitKey(d.key)
  const trades = useSort(d.trades, {
    when: t => t.exit_ts, token: t => (t.symbol ?? "").toLowerCase(), pnl: t => t.pnl_sol, pct: t => t.pnl_pct,
    peak: t => t.mfe_x, hold: t => t.hold_s, exit: t => t.exit_reason,
  }, "agent-trades")
  const config = { eq: { label: "Wallet", color: "var(--chart-1)" } } satisfies ChartConfig
  const chart = d.curve.map(([t, v, sym, p]) => ({ t, eq: v * 100, sym, p }))
  return (
    <>
      <SheetHeader>
        <SheetTitle className="flex flex-wrap items-center gap-2"><StrategyName k={d.key} /><RoleBadge role={d.status} /></SheetTitle>
        <SheetDescription>
          Wallet counted since {dayClock(d.since)}
          {d.lifetime && d.lifetime.trades !== d.trades_n ? ` · lifetime: ${d.lifetime.trades} trades, ${signedSol(d.lifetime.pnl_sol)} SOL` : ""}
          {d.note ? ` · ${d.note}` : ""}
        </SheetDescription>
      </SheetHeader>
      <div className="space-y-6 px-4 pb-8">
        <div className="grid grid-cols-3 gap-2 text-sm sm:grid-cols-6">
          <Mini label="Wallet" value={pct(d.equity * 100, 0)} className={tone(d.equity - 1)} />
          <Mini label="Profit" value={`${signedSol(d.pnl_sol)} SOL`} className={tone(d.pnl_sol)} />
          <Mini label="In USD" value={signedUsd(d.pnl_usd)} className={tone(d.pnl_usd)} />
          <Mini label="Trades" value={String(d.trades_n)} />
          <Mini label="Won" value={d.win == null ? "—" : pct(d.win * 100, 0)} />
          <Mini label="Best / worst" value={`${pct(d.best_trade, 0, true)} / ${pct(d.worst_trade, 0, true)}`} />
        </div>

        <Block title="Wallet, trade by trade">
          {chart.length < 2 ? <Empty>No closed trades yet.</Empty> : (
            <ChartContainer config={config} className="aspect-auto h-56 w-full">
              <LineChart data={chart} margin={{ left: 4, right: 8, top: 8 }}>
                <CartesianGrid vertical={false} />
                <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} tickFormatter={dayClock} tickLine={false} axisLine={false} minTickGap={70} />
                <YAxis tickFormatter={v => `${Math.round(v)}%`} tickLine={false} axisLine={false} width={46} domain={[0, "auto"]} />
                <ReferenceLine y={100} stroke="var(--muted-foreground)" strokeDasharray="4 4" />
                <ChartTooltip content={({ active, payload }) => {
                  const p = active && payload?.[0]?.payload
                  if (!p) return null
                  return (
                    <div className="rounded-md border bg-popover px-2.5 py-1.5 text-xs shadow">
                      <div className="text-muted-foreground">{dayClock(p.t)}</div>
                      {p.sym && <div><span className="font-medium">{p.sym}</span> <span className={tone(p.p)}>{pct(p.p, 0, true)}</span></div>}
                      <div className="num">wallet {Number(p.eq).toFixed(0)}%</div>
                    </div>
                  )
                }} />
                <Line dataKey="eq" type="stepAfter" stroke="var(--color-eq)" strokeWidth={2} dot={false} isAnimationActive={false} />
              </LineChart>
            </ChartContainer>
          )}
        </Block>

        {d.insight && (
          <Block title="Why it's winning" hint="Learned each generation from its live trades and passed on to new swarm agents.">
            <div className="space-y-3 text-sm">
              {d.insight.keepers.length > 0 && (
                <div>
                  <div className="text-xs text-muted-foreground">Its winning trades had this in common (new agents inherit it as a rule):</div>
                  <ul className="mt-1 space-y-1">
                    {d.insight.keepers.map((k, i) => (
                      <li key={i} className="font-mono text-xs">
                        buy only when {k.feature} {k.op} {fmtVal(k.value)}
                        <span className="font-sans text-muted-foreground"> · keeps {pct(k.keeps_profit * 100, 0)} of its profit, would have saved {k.saves_sol.toFixed(3)} SOL</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              <div>
                <div className="text-xs text-muted-foreground">Where the money came from:</div>
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {d.insight.exits.map(e => (
                    <span key={e.exit} className={cn("rounded px-2 py-0.5 text-xs", e.pnl_sol >= 0 ? "bg-positive/15 text-positive" : "bg-negative/15 text-negative")}>
                      {e.exit}: {signedSol(e.pnl_sol)} SOL ({e.trades})
                    </span>
                  ))}
                </div>
              </div>
              <div className="num text-xs text-muted-foreground">
                Holds winners {duration(d.insight.hold_win_s)} and losers {duration(d.insight.hold_loss_s)} (median) ·
                sells {d.insight.ladder.map(([x, p]) => `${p}% at ${x}x`).join(", ")} · stop −{Number(d.insight.stop_loss_pct ?? 0).toFixed(0)}% · bets {d.insight.bet_pct}% of the wallet
              </div>
            </div>
          </Block>
        )}

        <Block title="Stress test" hint="Replayed over the last 24 hours under worse conditions. Promotion needs it to survive most of them.">
          {!d.stress ? <Empty>Not tested yet. Live winners are stress-tested every generation.</Empty> : (
            <div className="space-y-2">
              <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-sm">
                <span>Robustness <span className={cn("num font-semibold", d.stress.robustness >= 0.6 ? "text-positive" : "text-negative")}>{pct(d.stress.robustness * 100, 0)}</span></span>
                <span>Chance its live edge is real <span className={cn("num font-semibold", d.stress.live_p_profit >= 0.8 ? "text-positive" : "text-warning")}>{pct(d.stress.live_p_profit * 100, 0)}</span></span>
                <span className="text-xs text-muted-foreground">gen {d.stress.gen}</span>
              </div>
              <div className="space-y-1">
                {Object.entries(d.stress.scenarios).map(([name, v]) => (
                  <div key={name} className="grid grid-cols-[1.25rem_minmax(0,1fr)_auto] items-center gap-2 text-xs">
                    <span className={v.passed ? "text-positive" : "text-negative"}>{v.passed ? "✓" : "✗"}</span>
                    <span className="truncate">{name}</span>
                    <span className={cn("num", tone(v.pnl_sol))}>{signedSol(v.pnl_sol)} SOL · {v.trades} trades</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Block>

        <Block title="What made it the best" hint="Profit by token. Share = this token's part of all winning profit.">
          {d.tokens.length === 0 ? <Empty>No trades yet.</Empty> : (
            <div className="space-y-1.5">
              {d.tokens.slice(0, 8).map(t => (
                <div key={t.mint} className="grid grid-cols-[minmax(0,1fr)_6rem_4.5rem] items-center gap-3 text-sm">
                  <div className="min-w-0">
                    <a className="truncate font-medium hover:underline" href={`https://dexscreener.com/solana/${t.mint}`} target="_blank" rel="noreferrer">{t.symbol}</a>
                    <div className="num text-xs text-muted-foreground">{t.trades} trade{t.trades > 1 ? "s" : ""} · peak {t.peak_x ? `${t.peak_x.toFixed(2)}x` : "—"} · best {pct(t.best_pct, 0, true)}</div>
                  </div>
                  <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                    <div className="h-full rounded-full bg-positive" style={{ width: `${Math.max(0, t.share_of_profit) * 100}%` }} />
                  </div>
                  <div className={cn("num text-right", tone(t.pnl_sol))}>{signedSol(t.pnl_sol)}</div>
                </div>
              ))}
            </div>
          )}
        </Block>

        <Block title="Family tree" hint="From this strategy back to the original. Each step shows what the swarm changed and how that version did live.">
          <ol className="relative space-y-3 border-l pl-5">
            {d.lineage.map((n, i) => (
              <li key={n.version} className="relative">
                <span className={cn("absolute -left-[25px] top-1.5 size-2.5 rounded-full ring-4 ring-popover", i === 0 ? "bg-chart-3" : n.status === "dead" ? "bg-negative" : "bg-muted-foreground")} />
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <button type="button" className="font-mono hover:underline" onClick={() => i > 0 && onOpen(`${sid}@${n.version}`)}>{sid}@{n.version.slice(0, 7)}</button>
                  <RoleBadge role={n.status} />
                  <span className="text-xs text-muted-foreground">{n.gen != null ? `gen ${n.gen}` : i === d.lineage.length - 1 ? "original" : ""} · {dayClock(n.created_ts)}</span>
                </div>
                <div className="num mt-0.5 text-xs text-muted-foreground">
                  live: {n.trades} trades, <span className={tone(n.pnl_sol)}>{signedSol(n.pnl_sol)} SOL</span>
                  {n.mate && <> · crossed with {n.mate.slice(0, 7)}</>}
                </div>
                {n.ops.length > 0 && (
                  <div className="mt-1 flex flex-wrap gap-1">
                    {n.ops.map((o, k) => <Badge key={k} variant="outline" className="font-mono text-[11px] font-normal">{opLabel(o)}</Badge>)}
                  </div>
                )}
              </li>
            ))}
          </ol>
          {d.children.length > 0 && (
            <div className="mt-3 text-xs text-muted-foreground">
              <span className="inline-flex items-center gap-1"><GitBranch className="size-3" />{d.children.length} children:</span>{" "}
              {d.children.map(c => (
                <button key={c.version} type="button" className="mr-2 font-mono hover:underline" onClick={() => onOpen(`${sid}@${c.version}`)}>
                  {c.version.slice(0, 7)} ({c.status})
                </button>
              ))}
            </div>
          )}
        </Block>

        {d.changes_vs_parent.length > 0 && (
          <Block title="What changed from its parent">
            <ul className="space-y-1 font-mono text-xs">{d.changes_vs_parent.map((c, i) => <li key={i}>• {c}</li>)}</ul>
          </Block>
        )}

        <Block title="Rules">
          {d.spec.description && <p className="mb-3 text-xs text-muted-foreground">{d.spec.description}</p>}
          <div className="grid gap-3 text-sm md:grid-cols-2">
            <div className="rounded-lg border p-3">
              <div className="mb-1.5 text-xs font-medium uppercase tracking-wide text-muted-foreground">Buys when</div>
              <ul className="space-y-1 font-mono text-xs">
                {d.spec.rules.filter(r => r.section !== "none").map(r => (
                  <li key={r.id}>{r.section === "any" ? "any of: " : ""}{r.feature} {r.op} {fmtVal(r.value)}</li>
                ))}
              </ul>
              <div className="mb-1.5 mt-3 text-xs font-medium uppercase tracking-wide text-muted-foreground">Never buys when</div>
              <ul className="space-y-1 font-mono text-xs">
                {d.spec.rules.filter(r => r.section === "none").map(r => (
                  <li key={r.id}>{r.feature} {r.op} {fmtVal(r.value)}{r.locked ? " 🔒" : ""}</li>
                ))}
              </ul>
              <div className="mt-3 text-xs text-muted-foreground">
                Tokens: {d.spec.universe.pools?.join(", ") ?? "any launchpad"} · {d.spec.universe.stage} · age {duration(d.spec.universe.min_age_s)}–{d.spec.universe.max_age_s ? duration(d.spec.universe.max_age_s) : "any"}
              </div>
            </div>
            <div className="rounded-lg border p-3">
              <div className="mb-1.5 text-xs font-medium uppercase tracking-wide text-muted-foreground">Sells</div>
              <ul className="space-y-1 text-xs">
                {d.spec.exits.take_profit.map(([x, p], i) => <li key={i}>take profit: sell {p}% at {Number(x).toFixed(2)}x</li>)}
                {d.spec.exits.stop_loss_pct != null && <li>stop loss at −{Number(d.spec.exits.stop_loss_pct).toFixed(0)}%</li>}
                {d.spec.exits.trailing_pct != null && <li>trailing stop {Number(d.spec.exits.trailing_pct).toFixed(0)}% once up {Number(d.spec.exits.trailing_activate_x).toFixed(2)}x</li>}
                {d.spec.exits.max_hold_s != null && <li>sell after {duration(d.spec.exits.max_hold_s)} at most</li>}
                {d.spec.exits.stale_after_s != null && <li>sell if no price for {duration(d.spec.exits.stale_after_s)}</li>}
                {d.spec.exits.on_rug && <li>sell on rug signs</li>}
                {d.spec.exits.on_dev_sell && <li>sell when the dev sells</li>}
                {d.spec.exits.on_kol_sell && <li>sell when a KOL sells</li>}
              </ul>
              <div className="mb-1.5 mt-3 text-xs font-medium uppercase tracking-wide text-muted-foreground">Bet size</div>
              <div className="text-xs">{d.spec.sizing.pct != null ? `${d.spec.sizing.pct}% of the wallet` : "the default share of the wallet"} · up to {d.spec.sizing.max_open} open</div>
              {Object.keys(d.spec.params).length > 0 && (
                <>
                  <div className="mb-1.5 mt-3 text-xs font-medium uppercase tracking-wide text-muted-foreground">Tunable settings</div>
                  <ul className="num space-y-0.5 text-xs">
                    {Object.entries(d.spec.params).map(([k, p]) => (
                      <li key={k}><span className="font-mono">{k}</span> = {fmtVal(p.value)} <span className="text-muted-foreground">(range {fmtVal(p.lo)}–{fmtVal(p.hi)})</span></li>
                    ))}
                  </ul>
                </>
              )}
            </div>
          </div>
        </Block>

        <Block title={`All trades (${d.trades.length})`}>
          {d.trades.length === 0 ? <Empty>No closed trades yet.</Empty> : (
            <Table>
              <TableHeader>
                <TableRow>
                  <SortHead k="when" {...trades}>Closed</SortHead>
                  <SortHead k="token" {...trades}>Token</SortHead>
                  <SortHead k="pct" right {...trades}>Result</SortHead>
                  <SortHead k="pnl" right {...trades}>SOL</SortHead>
                  <SortHead k="peak" right {...trades}>Peak</SortHead>
                  <SortHead k="hold" right {...trades}>Held</SortHead>
                  <TableHead>Exit</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {trades.sorted.map((t, i) => (
                  <TableRow key={i}>
                    <TableCell className="text-xs text-muted-foreground">{dayClock(t.exit_ts)}</TableCell>
                    <TableCell className="max-w-28 truncate">{t.symbol || t.mint.slice(0, 6)}</TableCell>
                    <TableCell className={cn("num text-right", tone(t.pnl_pct))}>{pct(t.pnl_pct, 0, true)}</TableCell>
                    <TableCell className={cn("num text-right", tone(t.pnl_sol))}>{signedSol(t.pnl_sol)}</TableCell>
                    <TableCell className="num text-right">{t.mfe_x ? `${t.mfe_x.toFixed(2)}x` : "—"}</TableCell>
                    <TableCell className="num text-right">{duration(t.hold_s)}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">{t.exit_reason ?? "—"}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Block>
      </div>
    </>
  )
}

function fmtVal(v: unknown): string {
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : Number(v.toPrecision(4)).toString()
  if (Array.isArray(v)) return `[${v.map(fmtVal).join(", ")}]`
  return String(v)
}

function Block({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section>
      <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{title}</h4>
      {hint && <p className="mt-0.5 text-xs text-muted-foreground/80">{hint}</p>}
      <div className="mt-2">{children}</div>
    </section>
  )
}

function Mini({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <div className="min-w-0 rounded-lg border p-2.5">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={cn("num mt-0.5 truncate font-medium", className)}>{value}</div>
    </div>
  )
}
