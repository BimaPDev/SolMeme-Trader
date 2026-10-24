import { Fragment, useMemo, useState } from "react"
import { ChevronRight } from "lucide-react"

import { ageSeconds, Empty, RoleBadge, Section, ShowMore, SortChips, SortHead, Stat, useSort } from "@/components/app/bits"
import { CoinLink } from "@/components/app/narratives"
import { WhaleTape } from "@/components/app/whale-tape"
import { Crowded } from "@/components/app/trades"
import { Badge } from "@/components/ui/badge"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import type { BoardRow, Position, Snapshot, TradesView } from "@/lib/api"
import { int, pct, usd } from "@/lib/format"
import { useInspect } from "@/lib/inspect"
import { cn } from "@/lib/utils"

const PHASE: Record<string, string> = {
  holding: "bg-chart-1/15 text-chart-1",
  bought: "bg-chart-1/15 text-chart-1",
  sold: "bg-chart-3/15 text-chart-3",
  checking: "bg-chart-2/15 text-chart-2",
  watching: "bg-muted text-muted-foreground",
  skipped: "bg-muted text-muted-foreground/70",
}

export type PositionGroup = {
  coin: string; mint: string; rows: Position[]; open: number; champion: boolean
  inUsd: boolean; inv: number; now: number; gain: number; money: (x: number) => string
}

const isClosed = (p: Position) => /^(closed|sold)/.test(p.status)

/** One entry per coin: many agents often hold the same coin, so the flat list repeats it dozens of times. */
export function groupPositions(positions: Position[]): PositionGroup[] {
  const by = new Map<string, Position[]>()
  for (const p of positions) {
    const k = p.mint || p.coin
    by.set(k, [...(by.get(k) ?? []), p])
  }
  return [...by.values()].map(rows => {
    const inUsd = rows.every(p => p.invested_usd != null && p.worth_usd != null)
    const inv = rows.reduce((a, p) => a + (inUsd ? p.invested_usd! : p.invested_sol), 0)
    const now = rows.reduce((a, p) => a + (inUsd ? p.worth_usd! : p.worth_sol), 0)
    return {
      coin: rows[0].coin, mint: rows[0].mint, rows, open: rows.filter(p => !isClosed(p)).length,
      champion: rows.some(p => p.role === "champion"), inUsd, inv, now, gain: now - inv,
      money: (x: number) => (inUsd ? usd(x) : `${x.toFixed(3)} SOL`),
    }
  })
}

type Who = "all" | "champion" | "swarm" | "other"
const WHO: [Who, string][] = [["all", "Everyone"], ["champion", "Champions"], ["swarm", "Swarm"], ["other", "Trials & challengers"]]
const whoOf = (p: Position): Who => (p.role === "champion" ? "champion" : p.role === "swarm" ? "swarm" : "other")

function gainTone(gain: number, inUsd: boolean) {
  return Math.abs(gain) < (inUsd ? 0.005 : 0.00005) ? "text-muted-foreground" : gain > 0 ? "text-positive" : "text-negative"
}

export function PositionsPage({ snap, trades }: { snap: Snapshot; trades: TradesView | null }) {
  const [who, setWho] = useState<Who>("all")
  const [state, setState] = useState<"open" | "all">("open")
  const [openRows, setOpenRows] = useState<Set<string>>(new Set())
  const mine = useMemo(() => snap.positions.filter(p => (who === "all" || whoOf(p) === who) && (state === "all" || !isClosed(p))),
    [snap.positions, who, state])
  const groups = useMemo(() => groupPositions(mine), [mine])
  const g = useSort(groups, {
    coin: x => x.coin.toLowerCase(), n: x => x.rows.length, invested: x => x.inv, worth: x => x.now, gain: x => x.gain,
    pct: x => (x.inv ? x.gain / x.inv : null),
  }, "position-groups")
  const sorted = useMemo(() => g.sort ? g.sorted : [...groups].sort((a, b) => b.now - a.now), [g.sort, g.sorted, groups])
  const all = groupPositions(snap.positions.filter(p => !isClosed(p)))
  const inv = all.reduce((a, x) => a + x.inv, 0)
  const now = all.reduce((a, x) => a + x.now, 0)
  const inUsd = all.every(x => x.inUsd)
  const money = (x: number) => (inUsd ? usd(x) : `${x.toFixed(3)} SOL`)
  const toggle = (k: string) => setOpenRows(cur => {
    const next = new Set(cur)
    if (next.has(k)) next.delete(k)
    else next.add(k)
    return next
  })

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
        <Stat label="Coins held" value={all.length} sub={`${snap.positions.filter(p => !isClosed(p)).length} open positions across all wallets`} />
        <Stat label="Put in" value={money(inv)} sub="open positions, all wallets" />
        <Stat label="Worth now" value={money(now)} tone={gainTone(now - inv, inUsd)} sub={<span className={gainTone(now - inv, inUsd)}>{now - inv >= 0 ? "+" : "−"}{money(Math.abs(now - inv))} ({inv ? pct(((now - inv) / inv) * 100, 0, true) : "—"})</span>} />
        <Stat label="Champions hold" value={all.filter(x => x.champion).length} sub="coins in the real (shared) wallet" />
      </div>

      <Crowded data={trades} />

      <Section title="Positions" description="One row per coin; tap it to see every agent holding it. Worth now = sold so far + the rest at market.">
        <div className="mb-3 flex flex-wrap items-center gap-1.5">
          {WHO.map(([k, label]) => <Chip key={k} on={who === k} onClick={() => setWho(k)}>{label}</Chip>)}
          <span className="mx-1 h-4 w-px bg-border" />
          <Chip on={state === "open"} onClick={() => setState("open")}>Open</Chip>
          <Chip on={state === "all"} onClick={() => setState("all")}>Incl. closed</Chip>
        </div>
        {sorted.length === 0 ? <Empty>No positions match.</Empty> : (
          <>
            <div className="md:hidden"><SortChips {...g} options={[["worth", "Worth"], ["gain", "Gain"], ["pct", "Gain %"], ["n", "Holders"], ["coin", "Coin"]]} /></div>
            <Table>
              <TableHeader>
                <TableRow>
                  <SortHead k="coin" {...g}>Coin</SortHead>
                  <SortHead k="n" right {...g} className="hidden sm:table-cell">Positions</SortHead>
                  <SortHead k="invested" right {...g} className="hidden sm:table-cell">Invested</SortHead>
                  <SortHead k="worth" right {...g}>Now</SortHead>
                  <SortHead k="gain" right {...g}>Gain</SortHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sorted.map(x => {
                  const k = x.mint || x.coin
                  const expanded = openRows.has(k)
                  const tn = gainTone(x.gain, x.inUsd)
                  return (
                    <Fragment key={k}>
                      <TableRow className="cursor-pointer" onClick={() => toggle(k)}>
                        <TableCell className="font-medium">
                          <span className="flex items-center gap-1.5">
                            <ChevronRight className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform", expanded && "rotate-90")} />
                            <span onClick={e => e.stopPropagation()}>
                              {x.mint ? <CoinLink mint={x.mint} className="underline decoration-muted-foreground/40 underline-offset-2 hover:decoration-foreground">{x.coin}</CoinLink> : x.coin}
                            </span>
                            {x.champion && <RoleBadge role="champion" />}
                            <span className="num text-xs text-muted-foreground sm:hidden">×{x.rows.length}</span>
                          </span>
                        </TableCell>
                        <TableCell className="num hidden text-right sm:table-cell">{x.rows.length}{x.open !== x.rows.length && <span className="text-muted-foreground"> ({x.open} open)</span>}</TableCell>
                        <TableCell className="num hidden text-right text-muted-foreground sm:table-cell">{x.money(x.inv)}</TableCell>
                        <TableCell className={cn("num text-right font-medium", tn)}>{x.money(x.now)}</TableCell>
                        <TableCell className={cn("num text-right", tn)}>
                          {x.gain >= 0 ? "+" : "−"}{x.money(Math.abs(x.gain))} <span className="text-xs">({x.inv ? pct((x.gain / x.inv) * 100, 0, true) : "—"})</span>
                        </TableCell>
                      </TableRow>
                      {expanded && x.rows.map((p, i) => <PositionRow key={i} p={p} />)}
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

function PositionRow({ p }: { p: Position }) {
  const inspect = useInspect()
  const inUsd = p.invested_usd != null && p.worth_usd != null
  const inv = inUsd ? p.invested_usd! : p.invested_sol
  const now = inUsd ? p.worth_usd! : p.worth_sol
  const gain = now - inv
  const money = (x: number) => (inUsd ? usd(x) : `${x.toFixed(3)} SOL`)
  const g = gainTone(gain, inUsd)
  return (
    <TableRow className="bg-muted/25 text-xs hover:bg-muted/40">
      <TableCell className="pl-8">
        <span className="flex flex-wrap items-center gap-1.5">
          <button type="button" className="text-muted-foreground hover:text-foreground hover:underline" onClick={() => p.key && inspect.agent(p.key)}>{p.strategy}</button>
          {p.role && <RoleBadge role={p.role} />}
          <Badge className={PHASE[p.status.split(" ")[0]] ?? "bg-muted text-muted-foreground"}>{p.status}</Badge>
          <span className="num text-muted-foreground">{p.mark}</span>
        </span>
      </TableCell>
      <TableCell className="hidden sm:table-cell" />
      <TableCell className="num hidden text-right text-muted-foreground sm:table-cell">{money(inv)}</TableCell>
      <TableCell className={cn("num text-right", g)}>{money(now)}</TableCell>
      <TableCell className={cn("num text-right", g)}>{gain >= 0 ? "+" : "−"}{money(Math.abs(gain))} ({inv ? pct((gain / inv) * 100, 0, true) : "—"})</TableCell>
    </TableRow>
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

export function ScannerPage({ snap }: { snap: Snapshot }) {
  const tok = useSort(snap.tokens, { token: t => t.symbol.toLowerCase(), doing: t => t.phase, age: t => ageSeconds(t.age) }, "tokens")
  return (
    <div className="space-y-4">
      <div className="grid gap-4 xl:grid-cols-2">
        <Section title="Tokens" description="What the bot is looking at right now">
          {snap.tokens.length === 0 ? <Empty>No tokens yet.</Empty> : (
            <Table>
              <TableHeader>
                <TableRow>
                  <SortHead k="token" {...tok}>Token</SortHead>
                  <SortHead k="doing" {...tok}>Doing</SortHead>
                  <TableHead className="hidden sm:table-cell">Detail</TableHead>
                  <SortHead k="age" right {...tok}>Age</SortHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {tok.sorted.map(t => (
                  <TableRow key={t.mint + t.ts}>
                    <TableCell>
                      <CoinLink mint={t.mint}>{t.symbol}</CoinLink>
                      <span className="ml-2 font-mono text-[11px] text-muted-foreground">{t.mint_short}</span>
                    </TableCell>
                    <TableCell><Badge className={PHASE[t.phase] ?? "bg-muted text-muted-foreground"}>{t.phase}</Badge></TableCell>
                    <TableCell className="hidden max-w-56 truncate text-muted-foreground sm:table-cell">{t.detail}</TableCell>
                    <TableCell className="num text-right text-muted-foreground">{t.age}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Section>

        <Section title="Feed" description="Latest decisions">
          {snap.feed.length === 0 ? <Empty>Feed is empty.</Empty> : (
            <ol className="max-h-[32rem] space-y-1 overflow-auto font-mono text-xs">
              {[...snap.feed].reverse().map((line, i) => (
                <li key={i} className={cn("truncate rounded px-2 py-1", i % 2 ? "" : "bg-muted/40",
                  /BUY|bought/i.test(line) ? "text-positive" : /SKIP/.test(line) ? "text-muted-foreground" : "")}>{line}</li>
              ))}
            </ol>
          )}
        </Section>
      </div>

      {snap.boards && (
        <div className="grid gap-4 xl:grid-cols-3">
          {(["new_creation", "near_completion", "completed"] as const).map(lane => {
            const rows: BoardRow[] = snap.boards?.trenches[lane]?.length ? snap.boards.trenches[lane]
              : snap.boards?.pump[{ new_creation: "new", near_completion: "almost", completed: "graduated" }[lane]] ?? []
            return (
              <Section key={lane} title={{ new_creation: "New", near_completion: "Almost bonded", completed: "Migrated" }[lane]}
                description={snap.boards?.trenches_source ? `from ${snap.boards.trenches_source}` : undefined}>
                {rows.length === 0 ? <Empty>Nothing here.</Empty> : (
                  <ul className="space-y-1.5 text-sm">
                    {rows.slice(0, 10).map(r => (
                      <li key={r.mint} className="flex items-center justify-between gap-2">
                        <span className="truncate font-medium">{r.symbol || r.mint.slice(0, 6)}</span>
                        <span className="num text-xs text-muted-foreground">
                          {usd(r.mcap_usd, 0)}{r.bonding_pct != null ? ` · ${pct(r.bonding_pct, 0)}` : ""}{r.holders != null ? ` · ${int(r.holders)} holders` : ""}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </Section>
            )
          })}
        </div>
      )}

    </div>
  )
}

export function WalletsPage({ snap }: { snap: Snapshot }) {
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
        <Stat label="Whales" value={snap.whales ? snap.whales.filter(w => w.status === "whale").length : "—"} sub="wallets that buy early and exit well" />
        <Stat label="Polled live" value={snap.whales ? snap.whales.filter(w => w.tracked).length : "—"} sub="top whales watched in real time" />
        <Stat label="Banned wallets" value={snap.bans ? int(snap.bans.count) : "—"} sub="pump-and-dump operators" />
        <Stat label="Tokens blocked" value={snap.bans ? int(snap.bans.tokens_blocked) : "—"} sub="skipped this session because of a ban" />
      </div>
      <WhaleTape />
      {snap.whales && (
        <Section title={`Whales (${snap.whales.filter(w => w.status === "whale").length})`}
          description="Wallets that keep buying good tokens early and exiting well. Re-scored every swarm generation; the top 10 are polled live, and strategies can use how many whales bought a token in the last 10 minutes.">
          {snap.whales.length === 0 ? <Empty>No whales yet. The first scan runs with the next swarm generation.</Empty> : (
            <ShowMore items={snap.whales} n={15}>{shown => (
            <div className="space-y-1.5">
              {shown.map(w => (
                <div key={w.address} className={cn("grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 rounded-md bg-muted/40 px-3 py-2 text-xs sm:grid-cols-[10rem_5rem_1fr_6rem]", w.status === "dropped" && "opacity-60")}>
                  <a className="font-mono hover:underline" href={`https://gmgn.ai/sol/address/${w.address}`} target="_blank" rel="noreferrer">{w.address.slice(0, 6)}…{w.address.slice(-4)}</a>
                  <Badge className={cn("justify-self-end capitalize sm:justify-self-start", w.status === "whale" ? "bg-positive/15 text-positive" : w.status === "watch" ? "bg-chart-2/15 text-chart-2" : "bg-muted text-muted-foreground")}>{w.status}{w.tracked ? " · polled" : ""}{w.council ? ` · ${w.council}` : ""}</Badge>
                  <span className="num text-muted-foreground">
                    {w.round_trips} trades · {w.win == null ? "—" : `${Math.round(w.win * 100)}% won`} · exit {w.median_exit == null ? "—" : `${w.median_exit.toFixed(2)}x`} · early on {w.early_good} good tokens
                    {w.flag && <span className="text-negative"> · {w.flag}</span>}
                  </span>
                  <span className="num justify-self-end font-medium">score {w.score.toFixed(2)}</span>
                </div>
              ))}
            </div>
            )}</ShowMore>
          )}
        </Section>
      )}

      {snap.bans && (
        <Section title={`Banned wallets (${snap.bans.count})`}
          description={`Pump-and-dump operators. Any token they create or buy is skipped by every strategy · ${snap.bans.tokens_blocked} tokens blocked this session. New suspects are found each swarm generation and approved by the council.`}>
          {snap.bans.recent.length === 0 ? <Empty>No wallets banned yet.</Empty> : (
            <ShowMore items={snap.bans.recent} n={12}>{shown => (
            <div className="space-y-1.5">
              {shown.map(b => (
                <div key={b.address} className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-0.5 rounded-md bg-muted/40 px-3 py-2 text-xs">
                  <a className="font-mono hover:underline" href={`https://solscan.io/account/${b.address}`} target="_blank" rel="noreferrer">{b.address.slice(0, 6)}…{b.address.slice(-4)}</a>
                  <Badge variant="outline" className="capitalize">{b.role}</Badge>
                  <span className="text-muted-foreground">{b.source}</span>
                  <span className="min-w-0 flex-1 text-muted-foreground [overflow-wrap:anywhere]">{b.reason}</span>
                </div>
              ))}
            </div>
            )}</ShowMore>
          )}
        </Section>
      )}

      <Section title="People" description={snap.people_note || "Wallets buying now, from the fomo leaderboard and tracked wallets"}>
        {snap.people.length === 0 ? <Empty>No traders yet.</Empty> : (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            {snap.people.map((p, i) => {
              const [head, ...rest] = p.text.split("\n")
              return (
                <div key={i} className="min-w-0 rounded-lg border p-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="min-w-0 truncate font-mono text-xs">{head}</span>
                    <Badge variant="outline" className="shrink-0 capitalize">{p.verdict}</Badge>
                  </div>
                  <pre className="mt-2 whitespace-pre-wrap text-xs text-muted-foreground [overflow-wrap:anywhere]">{rest.filter(l => !l.startsWith("verdict:")).join("\n")}</pre>
                </div>
              )
            })}
          </div>
        )}
      </Section>
    </div>
  )
}

export function Sources({ snap }: { snap: Snapshot }) {
  const ts = snap.trade_streams
  const src = useSort(snap.sources, { source: x => x.name, status: x => x.status, events: x => x.events, errors: x => x.errors }, "sources")
  return (
    <div className="grid gap-4 xl:grid-cols-3">
      <Section className="xl:col-span-2" title="Data sources" description={snap.scan_note ? `Last scan: ${snap.scan_note.split("\n")[0]}` : "Live feeds into the bot"}>
        <Table>
          <TableHeader>
            <TableRow>
              <SortHead k="source" {...src}>Source</SortHead>
              <SortHead k="status" {...src}>Status</SortHead>
              <SortHead k="events" right {...src}>Events</SortHead>
              <SortHead k="errors" right {...src}>Errors</SortHead>
              <TableHead>Note</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {src.sorted.map(s => (
              <TableRow key={s.name}>
                <TableCell className="font-medium">{s.name}</TableCell>
                <TableCell>
                  <Badge className={s.status === "ok" ? "bg-positive/15 text-positive" : s.status === "backoff" ? "bg-warning/15 text-warning" : "bg-negative/15 text-negative"}>{s.status}</Badge>
                </TableCell>
                <TableCell className="num text-right">{int(s.events)}</TableCell>
                <TableCell className={cn("num text-right", s.errors ? "text-warning" : "text-muted-foreground")}>{s.errors}</TableCell>
                <TableCell className="max-w-96 truncate text-muted-foreground">{s.note}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Section>
      <div className="space-y-4">
        <Section title="PumpPortal trade streams">
          {ts ? (
            <dl className="num grid grid-cols-2 gap-y-2 text-sm">
              <dt className="text-muted-foreground">Status</dt><dd className="text-right">{ts.unavailable ? "needs funded key" : ts.budget_exhausted ? "daily budget used" : "on"}</dd>
              <dt className="text-muted-foreground">Subscribed</dt><dd className="text-right">{ts.subscribed}</dd>
              <dt className="text-muted-foreground">Events today</dt><dd className="text-right">{int(ts.events_today)}</dd>
            </dl>
          ) : <Empty>Not connected.</Empty>}
        </Section>
        <Section title="Launch sniper" description="Paper only — learns from every launch">
          {snap.sniper ? (
            <dl className="num grid grid-cols-2 gap-y-2 text-sm">
              {Object.entries(snap.sniper).map(([k, v]) => (
                <div key={k} className="contents">
                  <dt className="text-muted-foreground">{k.replace(/_/g, " ")}</dt>
                  <dd className="text-right">{typeof v === "number" ? (Number.isInteger(v) ? int(v) : v.toFixed(3)) : String(v ?? "—")}</dd>
                </div>
              ))}
            </dl>
          ) : <Empty>Sniper is off.</Empty>}
        </Section>
      </div>
    </div>
  )
}
