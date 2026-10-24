import { useEffect, useMemo, useRef, useState, type ComponentType, type ReactNode } from "react"
import {
  Activity, ArrowLeftRight, BookOpen, Crosshair, MessageCircleQuestionMark, ShieldCheck, Users, Coins, Dna, LayoutDashboard, Menu, Newspaper, Radar, Radio, RefreshCw, Scale, Search,
  Trophy, Wallet, X, Zap,
} from "lucide-react"

import { Dot } from "@/components/app/bits"
import type { Snapshot, Swarm, TradesView } from "@/lib/api"
import { duration, int, pct, signedUsd, splitKey, tone, usd } from "@/lib/format"
import { useInspect } from "@/lib/inspect"
import { go, href, lastPaperPage, modeOf, type Page } from "@/lib/route"
import { cn } from "@/lib/utils"

type Icon = ComponentType<{ className?: string }>
export type NavItem = { page: Page; label: string; icon: Icon; hint: string }

export const NAV: { group: string; items: NavItem[] }[] = [
  {
    group: "Trading", items: [
      { page: "overview", label: "Overview", icon: LayoutDashboard, hint: "wallet, charts and what is happening now" },
      { page: "positions", label: "Positions", icon: Coins, hint: "every open and part-sold paper position" },
      { page: "trades", label: "Live trades", icon: ArrowLeftRight, hint: "every buy and sell as it fills" },
      { page: "strategies", label: "Strategies", icon: Trophy, hint: "champions, challengers and trials" },
    ],
  },
  {
    group: "Evolution", items: [
      { page: "swarm", label: "Swarm", icon: Dna, hint: "agents, the map and evolution" },
      { page: "council", label: "Council", icon: Scale, hint: "LLM judges, their chat and ideas" },
      { page: "learning", label: "Learning", icon: BookOpen, hint: "lessons, mutation scores and timeline" },
      { page: "ask", label: "Ask the council", icon: MessageCircleQuestionMark, hint: "paste a coin you found: buy, wait or avoid?" },
    ],
  },
  {
    group: "Market intel", items: [
      { page: "scanner", label: "Scanner", icon: Radar, hint: "tokens being checked, decisions, launch boards" },
      { page: "narratives", label: "Narratives", icon: Newspaper, hint: "themes, copycats, tweets and news" },
      { page: "wallets", label: "Wallets", icon: Wallet, hint: "whales, banned wallets and people" },
    ],
  },
  {
    group: "System", items: [
      { page: "sources", label: "Sources", icon: Radio, hint: "data feeds and their health" },
    ],
  },
]
/** Live mode: the real-size wallet (a dry run for now: real prices, no real money). */
export const LIVE_NAV: { group: string; items: NavItem[] }[] = [
  {
    group: "Live wallet", items: [
      { page: "live", label: "Overview", icon: Zap, hint: "the $50 wallet at real prices (dry run, no real money)" },
      { page: "live-positions", label: "Positions", icon: Coins, hint: "open and recent live positions, with their charts" },
      { page: "live-trades", label: "Trades", icon: ArrowLeftRight, hint: "every live buy, sell and failed attempt" },
    ],
  },
  {
    group: "Your bots", items: [
      { page: "live-team", label: "Team", icon: Users, hint: "ResearchBot, SniperBot, TraderBot, the council and the Overseer" },
      { page: "live-research", label: "ResearchBot", icon: Search, hint: "every strategy match, already checked: wallets, holders, liquidity" },
      { page: "live-setups", label: "SniperBot", icon: Crosshair, hint: "setups one condition away, and the trade ticket ready to fire" },
      { page: "live-trader", label: "TraderBot", icon: ShieldCheck, hint: "your permissions: strategies, bet size, limits" },
    ],
  },
  {
    group: "Brain (shared with paper)", items: [
      { page: "live-strategy", label: "Strategy", icon: Trophy, hint: "the champion the live wallet copies, and how it does live" },
      { page: "live-council", label: "Council", icon: Scale, hint: "the AI council that shapes the strategies" },
      { page: "live-ask", label: "Ask the council", icon: MessageCircleQuestionMark, hint: "paste a coin you found: buy, wait or avoid?" },
      { page: "live-whales", label: "Whales", icon: Wallet, hint: "wallets the council trusts, and how they move live bets" },
      { page: "live-scanner", label: "Scanner", icon: Radar, hint: "coins being checked right now, and why each was bought or skipped" },
      { page: "live-narratives", label: "Narratives", icon: Newspaper, hint: "themes, copycats, tweets and news the bot is matching" },
    ],
  },
]
export const ALL_NAV = [...NAV, ...LIVE_NAV].flatMap(g => g.items)
export const navItem = (p: Page) => ALL_NAV.find(i => i.page === p)!

/** Small live counts next to the nav labels. */
function useBadges(snap: Snapshot | null, swarm: Swarm | null): Partial<Record<Page, { text?: string; tone?: string; pulse?: boolean }>> {
  return useMemo(() => {
    if (!snap) return {}
    const open = snap.strategies.reduce((a, s) => a + s.open, 0)
    const bad = snap.sources.filter(s => s.status !== "ok").length
    const clock = snap.swarm_clock
    const sitting = !!clock?.running && clock.phase === "council"
    return {
      positions: open ? { text: String(open) } : undefined,
      swarm: clock ? { text: String(clock.agents), pulse: !!clock.running && !sitting } : undefined,
      council: sitting ? { text: "live", tone: "text-negative", pulse: true }
        : swarm?.council ? { text: String(swarm.council.ideas.filter(i => i.status === "working").length || "") || undefined } : undefined,
      sources: bad ? { text: String(bad), tone: "text-warning" } : undefined,
      "live-positions": snap.dryrun?.open ? { text: String(snap.dryrun.open) } : undefined,
      "live-trades": snap.dryrun?.trades ? { text: String(snap.dryrun.trades) } : undefined,
      "live-council": sitting ? { text: "live", tone: "text-negative", pulse: true } : undefined,
      wallets: snap.whales ? { text: String(snap.whales.filter(w => w.status === "whale").length) } : undefined,
    }
  }, [snap, swarm])
}

/** Paper (learn and test) or Live (the real-size wallet): the switch at the top of the sidebar and the top bar. */
export function ModeSwitch({ page, compact }: { page: Page; compact?: boolean }) {
  const live = modeOf(page) === "live"
  const btn = "flex flex-1 items-center justify-center gap-1.5 rounded-md font-medium transition-colors"
  return (
    <div role="tablist" aria-label="Paper or live" className={cn("flex rounded-lg border bg-muted/40 p-0.5", compact ? "text-[11px]" : "text-xs")}>
      <button type="button" role="tab" aria-selected={!live} onClick={() => live && go(lastPaperPage())}
        className={cn(btn, compact ? "px-2 py-0.5" : "px-2 py-1", !live ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground")}>
        Paper
      </button>
      <button type="button" role="tab" aria-selected={live} onClick={() => !live && go("live")}
        className={cn(btn, compact ? "px-2 py-0.5" : "px-2 py-1", live ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground")}>
        <span className={cn("size-1.5 rounded-full", live ? "animate-pulse bg-negative" : "bg-negative/60")} />Live
      </button>
    </div>
  )
}

function NavList({ page, snap, swarm, onPick }: { page: Page; snap: Snapshot | null; swarm: Swarm | null; onPick?: () => void }) {
  const badges = useBadges(snap, swarm)
  const live = modeOf(page) === "live"
  return (
    <nav className="space-y-5">
      {live && (
        <p className="px-3 text-xs text-muted-foreground">The learning side (swarm, council, strategies) is under <span className="font-medium text-foreground">Paper</span>: the switch on the wallet card.</p>
      )}
      {(live ? LIVE_NAV : NAV).map(g => (
        <div key={g.group}>
          <div className="mb-1 px-3 text-[11px] font-medium uppercase tracking-wider text-muted-foreground/70">{g.group}</div>
          <ul className="space-y-0.5">
            {g.items.map(it => {
              const on = it.page === page
              const b = badges[it.page]
              const I = it.icon
              return (
                <li key={it.page}>
                  <a href={href(it.page)} onClick={onPick} aria-current={on ? "page" : undefined}
                    className={cn("group flex items-center gap-2.5 rounded-lg px-3 py-1.5 text-sm transition-colors",
                      on ? "bg-primary/12 font-medium text-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground")}>
                    <I className={cn("size-4 shrink-0", on ? "text-primary" : "opacity-80")} />
                    <span className="flex-1 truncate">{it.label}</span>
                    {b?.text && (
                      <span className={cn("num inline-flex items-center gap-1 rounded-md px-1.5 text-[11px]", on ? "bg-primary/15 text-primary" : "bg-muted", b.tone)}>
                        {b.pulse && <span className="size-1.5 animate-pulse rounded-full bg-current" />}{b.text}
                      </span>
                    )}
                  </a>
                </li>
              )
            })}
          </ul>
        </div>
      ))}
    </nav>
  )
}

function Brand({ page }: { page: Page }) {
  const live = modeOf(page) === "live"
  return (
    <a href={href(live ? "live" : "overview")} className="flex items-center gap-2.5">
      <div className={cn("flex size-8 items-center justify-center rounded-lg", live ? "bg-negative/15 text-negative" : "bg-primary/15 text-primary")}>
        {live ? <Zap className="size-4" /> : <Dna className="size-4" />}
      </div>
      <div>
        <div className="font-semibold leading-tight">SolMeme</div>
        <div className="text-[11px] leading-tight text-muted-foreground">{live ? "live · dry run, no real money" : "paper only · no real trades"}</div>
      </div>
    </a>
  )
}

/** The wallet at the bottom of the sidebar (and atop the phone menu), with the Paper / Live switch:
 * Paper shows the champions' wallet and its goal, Live the real-size wallet (a dry run for now). */
function WalletCard({ snap, page }: { snap: Snapshot | null; page: Page }) {
  const live = modeOf(page) === "live"
  return (
    <div className={cn("rounded-xl border bg-card/60 p-3", live && "border-negative/30")}>
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          {live ? "Live wallet" : "Champions' wallet"}
        </span>
        <ModeSwitch page={page} compact />
      </div>
      {live ? <LiveWalletBody snap={snap} /> : <PaperWalletBody snap={snap} />}
    </div>
  )
}

function PaperWalletBody({ snap }: { snap: Snapshot | null }) {
  const w = snap?.wallet
  if (!w) return <div className="mt-1 text-xs text-muted-foreground">Waiting for the bot…</div>
  const goal = Math.max(0, Math.min(100, w.goal_pct ?? 0))
  return (
    <a href={href("overview")} className="block">
      <div className="num mt-0.5 flex items-baseline gap-2">
        <span className={cn("text-xl font-semibold", tone(w.pnl_usd))}>{usd(w.balance_usd)}</span>
        <span className={cn("text-xs", tone(w.pnl_pct))}>{pct(w.pnl_pct, 1, true)}</span>
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted">
        <div className="h-full rounded-full bg-positive transition-[width] duration-700" style={{ width: `${goal}%` }} />
      </div>
      <div className="num mt-1 flex justify-between text-[11px] text-muted-foreground">
        <span>goal {usd(w.goal_usd, 0)}</span><span>{pct(w.goal_pct, 0)}</span>
      </div>
    </a>
  )
}

function LiveWalletBody({ snap }: { snap: Snapshot | null }) {
  const d = snap?.dryrun
  if (!d) return <div className="mt-1 text-xs text-muted-foreground">Starts with the bot's next restart: a fresh $50 at real prices.</div>
  const toGoal = Math.max(0, Math.min(100, d.equity_usd != null ? (100 * d.equity_usd) / (2 * d.start_usd) : 0))
  return (
    <a href={href("live")} className="block">
      <div className="num mt-0.5 flex items-baseline gap-2">
        <span className={cn("text-xl font-semibold", tone(d.pnl_pct))}>{usd(d.equity_usd)}</span>
        <span className={cn("text-xs", tone(d.pnl_pct))}>{pct(d.pnl_pct, 1, true)}</span>
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted">
        <div className="h-full rounded-full bg-positive transition-[width] duration-700" style={{ width: `${toGoal}%` }} />
      </div>
      <div className="num mt-1 flex justify-between text-[11px] text-muted-foreground">
        <span>goal {usd(2 * d.start_usd, 0)} · {d.trades} trades</span><span>{pct(d.equity_usd != null ? (100 * d.equity_usd) / (2 * d.start_usd) : null, 0)}</span>
      </div>
    </a>
  )
}

export function Sidebar({ page, snap, swarm }: { page: Page; snap: Snapshot | null; swarm: Swarm | null }) {
  return (
    <aside className="fixed inset-y-0 left-0 z-30 hidden w-60 flex-col border-r bg-sidebar lg:flex">
      <div className="px-4 py-4"><Brand page={page} /></div>
      <div className="flex-1 overflow-y-auto px-2 pb-4">
        <NavList page={page} snap={snap} swarm={swarm} />
      </div>
      <div className="border-t p-3"><WalletCard snap={snap} page={page} /></div>
    </aside>
  )
}

/** Phone: the four pages people check most, plus a menu with everything. */
const QUICK: Page[] = ["overview", "positions", "trades", "swarm"]
const LIVE_QUICK: Page[] = ["live", "live-positions", "live-trades", "live-council"]

export function MobileNav({ page, snap, swarm }: { page: Page; snap: Snapshot | null; swarm: Swarm | null }) {
  const [open, setOpen] = useState(false)
  useEffect(() => { setOpen(false) }, [page])
  const badges = useBadges(snap, swarm)
  const quick = modeOf(page) === "live" ? LIVE_QUICK : QUICK
  return (
    <>
      <nav className="fixed inset-x-0 bottom-0 z-40 transform-gpu border-t bg-background pb-[env(safe-area-inset-bottom)] lg:hidden">
        <ul className="grid grid-cols-5">
          {quick.map(p => {
            const it = navItem(p)
            const I = it.icon
            const on = p === page
            const b = badges[p]
            return (
              <li key={p}>
                <a href={href(p)} className={cn("relative flex flex-col items-center gap-0.5 py-2 text-[11px]", on ? "text-primary" : "text-muted-foreground")}>
                  <I className="size-5" />
                  {p === "live" ? "Live" : it.label.replace("Live trades", "Trades")}
                  {b?.text && <span className={cn("num absolute right-[18%] top-1 rounded-full bg-muted px-1 text-[9px] leading-tight", b.tone)}>{b.text}</span>}
                </a>
              </li>
            )
          })}
          <li>
            <button type="button" onClick={() => setOpen(true)}
              className={cn("flex w-full flex-col items-center gap-0.5 py-2 text-[11px]", quick.includes(page) ? "text-muted-foreground" : "text-primary")}>
              <Menu className="size-5" />More
            </button>
          </li>
        </ul>
      </nav>
      {open && (
        <div className="fixed inset-0 z-50 lg:hidden">
          <div className="absolute inset-0 bg-black/60" onClick={() => setOpen(false)} />
          <div className="absolute inset-x-0 bottom-0 max-h-[85svh] overflow-y-auto rounded-t-2xl border-t bg-popover p-4 pb-[calc(1rem+env(safe-area-inset-bottom))] animate-in slide-in-from-bottom-8 fade-in">
            <div className="mb-4 flex items-center justify-between">
              <Brand page={page} />
              <button type="button" onClick={() => setOpen(false)} className="rounded-md p-1.5 hover:bg-muted" aria-label="Close menu"><X className="size-5" /></button>
            </div>
            <WalletCard snap={snap} page={page} />
            <div className="mt-4"><NavList page={page} snap={snap} swarm={swarm} onPick={() => setOpen(false)} /></div>
          </div>
        </div>
      )}
    </>
  )
}

export function TopBar({ page, snap, ok, error, onSync, syncing, syncedAt, onSearch }: {
  page: Page; snap: Snapshot | null; ok: boolean; error: string
  onSync: () => void; syncing: boolean; syncedAt: number | null; onSearch: () => void
}) {
  const it = navItem(page)
  const group = [...NAV, ...LIVE_NAV].find(g => g.items.includes(it))?.group
  const w = snap?.wallet
  const live = modeOf(page) === "live"
  const d = snap?.dryrun
  return (
    <header className="sticky top-0 z-20 border-b bg-background pt-[env(safe-area-inset-top)] lg:bg-background/85 lg:backdrop-blur">
      <div className="flex items-center gap-3 px-4 py-2.5 sm:px-6">
        <div className="min-w-0">
          <div className="text-[11px] leading-tight text-muted-foreground">{group}</div>
          <h1 className="truncate text-base font-semibold leading-tight">{it.label}</h1>
        </div>
        {snap && live && (
          <dl className="num ml-4 hidden items-center gap-x-5 text-sm whitespace-nowrap xl:flex">
            <HeaderStat label="Live wallet" value={d ? usd(d.equity_usd) : "not started"} sub={d ? pct(d.pnl_pct, 1, true) : undefined} subTone={tone(d?.pnl_pct)} />
            {d && <HeaderStat label="Paper, same period" value={pct(d.paper_pnl_pct, 1, true)} />}
            <HeaderStat label="SOL" value={usd(snap.sol_usd)} />
          </dl>
        )}
        {snap && !live && (
          <dl className="num ml-4 hidden items-center gap-x-5 text-sm whitespace-nowrap xl:flex">
            {w?.locked_usd != null && w.start_usd != null && (
              <HeaderStat label="Locked in" value={usd(w.start_usd + w.locked_usd)} sub={signedUsd(w.locked_usd)} subTone={tone(w.locked_usd)} />
            )}
            <HeaderStat label="SOL" value={usd(snap.sol_usd)} />
            <HeaderStat label="Uptime" value={duration(snap.uptime_s)} />
            <HeaderStat label="Events" value={int(snap.events)} />
          </dl>
        )}
        <div className="ml-auto flex items-center gap-2 text-sm">
          {live ? d && (
            <span className={cn("num font-medium lg:hidden", tone(d.pnl_pct))}>{usd(d.equity_usd)}</span>
          ) : w && (
            <span className={cn("num font-medium lg:hidden", tone(w.pnl_usd))}>{usd(w.balance_usd)}</span>
          )}
          <button type="button" onClick={onSearch}
            className="inline-flex h-7 items-center gap-2 rounded-lg border bg-muted/40 px-2.5 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground">
            <Search className="size-3.5" /><span className="hidden sm:inline">Search</span>
            <kbd className="hidden rounded border bg-background px-1 font-sans text-[10px] sm:inline">⌘K</kbd>
          </button>
          {!ok && <span className="hidden text-xs text-negative sm:inline">{error}</span>}
          <button type="button" onClick={onSync} disabled={syncing} aria-label="Sync all prices and stats"
            title={syncedAt ? `Last synced ${new Date(syncedAt).toLocaleTimeString()}` : "Sync all prices and stats now"}
            className="inline-flex h-7 items-center gap-1.5 rounded-lg border px-2.5 text-xs font-medium transition-colors hover:bg-muted disabled:opacity-70">
            <RefreshCw className={cn("size-3", syncing && "animate-spin")} /><span className="hidden sm:inline">{syncing ? "syncing" : "sync"}</span>
          </button>
          <span className="inline-flex h-7 items-center gap-1.5 rounded-lg border px-2.5 text-xs" title={ok ? "Receiving live data" : error}>
            <Dot ok={ok && !!snap} /><span className="hidden sm:inline">{ok && snap ? "live" : "offline"}</span>
          </span>
        </div>
      </div>
    </header>
  )
}

function HeaderStat({ label, value, sub, subTone }: { label: string; value: string; sub?: string; subTone?: string }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="font-medium">{value}</dd>
      {sub && <dd className={cn("text-xs", subTone ?? "text-muted-foreground")}>{sub}</dd>}
    </div>
  )
}

// ── search: pages, strategies, agents and coins ──────────────────────────────
type Hit = { id: string; kind: "page" | "strategy" | "coin"; label: ReactNode; text: string; sub: string; icon: Icon; run: () => void }

export function CommandPalette({ open, onClose, snap, swarm, trades }: {
  open: boolean; onClose: () => void; snap: Snapshot | null; swarm: Swarm | null; trades: TradesView | null
}) {
  const [q, setQ] = useState("")
  const [sel, setSel] = useState(0)
  const input = useRef<HTMLInputElement>(null)
  const list = useRef<HTMLUListElement>(null)
  const inspect = useInspect()

  useEffect(() => {
    if (open) { setQ(""); setSel(0); setTimeout(() => input.current?.focus(), 0) }
  }, [open])

  const all = useMemo<Hit[]>(() => {
    const out: Hit[] = ALL_NAV.map(it => ({
      id: `p:${it.page}`, kind: "page", label: it.label, text: `${it.label} ${it.hint}`, sub: it.hint, icon: it.icon, run: () => go(it.page),
    }))
    const seen = new Set<string>()
    const addStrategy = (key: string, sub: string) => {
      if (seen.has(key)) return
      seen.add(key)
      const [id, v] = splitKey(key)
      out.push({ id: `s:${key}`, kind: "strategy", label: <>{id} <span className="font-mono text-[11px] text-muted-foreground">{v}</span></>,
        text: key, sub, icon: Dna, run: () => inspect.agent(key) })
    }
    for (const s of snap?.strategies ?? []) addStrategy(s.key, `${s.role} · ${s.closed} closed · ${s.open} open`)
    for (const a of swarm?.agents ?? []) addStrategy(a.key, `${a.status === "swarm" ? "alive" : a.status} · gen ${a.gen} · ${a.live_n} trades`)
    const coins = new Map<string, string>()
    for (const p of snap?.positions ?? []) if (p.mint) coins.set(p.mint, p.coin)
    for (const t of snap?.tokens ?? []) if (t.mint && !coins.has(t.mint)) coins.set(t.mint, t.symbol)
    for (const t of trades?.tape ?? []) if (t.mint && !coins.has(t.mint)) coins.set(t.mint, t.symbol)
    for (const [mint, sym] of coins) {
      out.push({ id: `c:${mint}`, kind: "coin", label: sym || mint.slice(0, 6), text: `${sym} ${mint}`, sub: `${mint.slice(0, 4)}…${mint.slice(-4)}`,
        icon: Activity, run: () => inspect.coin(mint) })
    }
    return out
  }, [snap, swarm, trades, inspect])

  const hits = useMemo(() => {
    const words = q.toLowerCase().trim().split(/\s+/).filter(Boolean)
    const match = all.filter(h => words.every(w => h.text.toLowerCase().includes(w)))
    return words.length ? match.slice(0, 40) : match.filter(h => h.kind === "page")
  }, [q, all])

  useEffect(() => { setSel(0) }, [q])
  useEffect(() => {
    list.current?.querySelector<HTMLElement>(`[data-i="${sel}"]`)?.scrollIntoView({ block: "nearest" })
  }, [sel])

  if (!open) return null
  const pick = (h: Hit) => { onClose(); h.run() }
  const groups: [Hit["kind"], string][] = [["page", "Pages"], ["strategy", "Strategies & agents"], ["coin", "Coins"]]
  let i = -1
  return (
    <div className="fixed inset-0 z-[60] flex items-start justify-center p-3 pt-[10svh]">
      <div className="absolute inset-0 bg-black/60 animate-in fade-in" onClick={onClose} />
      <div className="relative w-full max-w-xl overflow-hidden rounded-xl border bg-popover shadow-2xl animate-in fade-in zoom-in-95">
        <div className="flex items-center gap-2 border-b px-3">
          <Search className="size-4 text-muted-foreground" />
          <input ref={input} value={q} onChange={e => setQ(e.target.value)} placeholder="Jump to a page, strategy, agent or coin…"
            className="h-12 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
            onKeyDown={e => {
              if (e.key === "ArrowDown") { e.preventDefault(); setSel(s => Math.min(hits.length - 1, s + 1)) }
              else if (e.key === "ArrowUp") { e.preventDefault(); setSel(s => Math.max(0, s - 1)) }
              else if (e.key === "Enter" && hits[sel]) { e.preventDefault(); pick(hits[sel]) }
              else if (e.key === "Escape") onClose()
            }} />
          <kbd className="rounded border px-1.5 text-[10px] text-muted-foreground">esc</kbd>
        </div>
        <ul ref={list} className="max-h-[60svh] overflow-y-auto p-1.5">
          {hits.length === 0 && <li className="px-3 py-6 text-center text-sm text-muted-foreground">Nothing matches “{q}”.</li>}
          {groups.map(([kind, title]) => {
            const mine = hits.filter(h => h.kind === kind)
            if (!mine.length) return null
            return (
              <li key={kind}>
                <div className="px-2.5 pb-1 pt-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground/70">{title}</div>
                <ul>
                  {mine.map(h => {
                    i += 1
                    const k = i
                    const I = h.icon
                    return (
                      <li key={h.id} data-i={k}>
                        <button type="button" onMouseMove={() => setSel(k)} onClick={() => pick(h)}
                          className={cn("flex w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left text-sm", sel === k && "bg-muted")}>
                          <I className="size-4 shrink-0 text-muted-foreground" />
                          <span className="min-w-0 flex-1 truncate">{h.label}</span>
                          <span className="hidden max-w-[50%] truncate text-xs text-muted-foreground sm:block">{h.sub}</span>
                        </button>
                      </li>
                    )
                  })}
                </ul>
              </li>
            )
          })}
        </ul>
      </div>
    </div>
  )
}

/** A card-sized link to a detail page, used on the overview. */
export function MoreLink({ page, children }: { page: Page; children?: ReactNode }) {
  return (
    <a href={href(page)} className="inline-flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground">
      {children ?? "view all"} →
    </a>
  )
}
