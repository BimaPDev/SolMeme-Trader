import { useEffect, useState } from "react"

import { CouncilView } from "@/components/app/council"
import { AskPage } from "@/components/app/ask"
import { TeamPage } from "@/components/app/team"
import { ResearchPage, SetupsPage, TraderPage } from "@/components/app/workflow"
import { LiveCouncilPage, LivePositionsPage, LivePage, LiveStrategyPage, LiveTradesPage, LiveWhalesPage } from "@/components/app/live"
import { NarrativesView } from "@/components/app/narratives"
import { PositionsPage, ScannerPage, Sources, WalletsPage } from "@/components/app/market"
import { Overview, StrategiesPage } from "@/components/app/overview"
import { CommandPalette, MobileNav, Sidebar, TopBar } from "@/components/app/shell"
import { LearningPage, SwarmView } from "@/components/app/swarm"
import { TradesPage } from "@/components/app/trades"
import { Empty } from "@/components/app/bits"
import { Skeleton } from "@/components/ui/skeleton"
import { usePoll, type History, type LiveView, type SessionReport, type Snapshot, type Swarm, type TradesView } from "@/lib/api"
import { modeOf, useRoute } from "@/lib/route"

export default function App() {
  const page = useRoute()
  const snap = usePoll<Snapshot>("api/snapshot", 1000)
  const history = usePoll<History>("api/history", 15000)
  const swarm = usePoll<Swarm>("api/swarm", 15000, 15000)
  const report = usePoll<SessionReport>("api/session-report", 30000)
  const trades = usePoll<TradesView>("api/trades", 4000)
  const liveMode = modeOf(page) === "live"
  const live = usePoll<LiveView>("api/live", liveMode ? 5000 : 60000, 15000)   // the live wallet, fast only when shown
  const s = snap.data
  const [syncing, setSyncing] = useState(false)
  const [syncedAt, setSyncedAt] = useState<number | null>(null)
  const [search, setSearch] = useState(false)
  const sync = async () => {
    if (syncing) return
    setSyncing(true)
    try {
      await Promise.allSettled([snap.refresh(), history.refresh(), swarm.refresh(), report.refresh(), trades.refresh(), live.refresh()])
      setSyncedAt(Date.now())
    } finally {
      setSyncing(false)
    }
  }

  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setSearch(v => !v) }
      else if (e.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test((e.target as HTMLElement)?.tagName)) { e.preventDefault(); setSearch(true) }
    }
    window.addEventListener("keydown", on)
    return () => window.removeEventListener("keydown", on)
  }, [])

  const sw = swarm.data
  const body = !s ? <Loading /> : (() => {
    switch (page) {
      case "overview": return <Overview snap={s} history={history.data} swarm={sw} trades={trades.data} />
      case "positions": return <PositionsPage snap={s} trades={trades.data} />
      case "trades": return <TradesPage data={trades.data} />
      case "strategies": return <StrategiesPage snap={s} history={history.data} report={report.data} />
      case "swarm": return <SwarmView snap={s} swarm={sw} trades={trades.data} />
      case "council": return !sw ? <Empty>Loading the swarm…</Empty> : sw.error ? <Empty>Swarm data unavailable: {sw.error}</Empty> : <CouncilView swarm={sw} snap={s} />
      case "learning": return <LearningPage swarm={sw} />
      case "scanner": return <ScannerPage snap={s} />
      case "narratives": return <NarrativesView />
      case "wallets": return <WalletsPage snap={s} />
      case "sources": return <Sources snap={s} />
      case "live": return <LivePage snap={s} live={live} />
      case "live-positions": return <LivePositionsPage snap={s} live={live} />
      case "live-trades": return <LiveTradesPage snap={s} live={live} />
      case "live-strategy": return <LiveStrategyPage snap={s} live={live} swarm={sw} />
      case "live-council": return <LiveCouncilPage snap={s} swarm={sw} />
      case "live-whales": return <LiveWhalesPage snap={s} live={live} />
      case "live-scanner": return <ScannerPage snap={s} />
      case "live-narratives": return <NarrativesView />
      case "ask": case "live-ask": return <AskPage />
      case "live-team": return <TeamPage />
      case "live-research": return <ResearchPage />
      case "live-setups": return <SetupsPage />
      case "live-trader": return <TraderPage snap={s} />
    }
  })()

  return (
    <div className="min-h-svh bg-background">
      <Sidebar page={page} snap={s} swarm={sw} />
      <div className="lg:pl-60">
        <TopBar page={page} snap={s} ok={snap.ok} error={snap.error} onSync={sync} syncing={syncing} syncedAt={syncedAt} onSearch={() => setSearch(true)} />
        <main key={page} className="mx-auto max-w-[1600px] px-3 py-4 pb-[calc(6rem+env(safe-area-inset-bottom))] sm:px-6 sm:py-5 lg:pb-8">{body}</main>
      </div>
      <MobileNav page={page} snap={s} swarm={sw} />
      <CommandPalette open={search} onClose={() => setSearch(false)} snap={s} swarm={sw} trades={trades.data} />
    </div>
  )
}

function Loading() {
  return (
    <div className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">{[0, 1, 2, 3].map(i => <Skeleton key={i} className="h-28 rounded-xl" />)}</div>
      <Skeleton className="h-72 rounded-xl" />
      <p className="text-center text-sm text-muted-foreground">Waiting for the bot…</p>
    </div>
  )
}
