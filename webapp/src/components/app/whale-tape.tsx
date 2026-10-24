import { Empty, Section } from "@/components/app/bits"
import { CoinLink } from "@/components/app/narratives"
import { Badge } from "@/components/ui/badge"
import { usePoll } from "@/lib/api"
import { ago, usd } from "@/lib/format"
import { cn } from "@/lib/utils"

type TapeRow = {
  ts: number; wallet: string; mint: string; symbol: string; side: string; sol?: number | null; x_since?: number | null; source?: string
  whale?: { score?: number; win?: number; council?: string; label?: string }
}
type Cluster = { mint: string; symbol: string; whales: number; trusted: number; first_ts: number; mcap_usd?: number | null }
type Tape = { trades?: TapeRow[]; clusters?: Cluster[]; whales_watched?: number; error?: string }

const COUNCIL: Record<string, string> = { trust: "bg-positive/15 text-positive", distrust: "bg-negative/15 text-negative", unsure: "bg-chart-2/15 text-chart-2" }

/** What the whales are trading right now: every pump.fun trade by a whale in the book, live. */
export function WhaleTape() {
  const { data } = usePoll<Tape>("api/whale-tape", 5000)
  const trades = data?.trades ?? []
  const clusters = data?.clusters ?? []
  return (
    <div className="space-y-4">
      <Section title="Coins several whales are buying" description="3 or more whales from the book bought the same coin in the last 10 minutes.">
        {clusters.length === 0 ? <Empty>No whale cluster right now.</Empty> : (
          <ul className="divide-y">
            {clusters.map(c => (
              <li key={c.mint} className="flex flex-wrap items-center gap-2 py-1.5 text-sm">
                <CoinLink mint={c.mint}>{c.symbol}</CoinLink>
                <Badge className="bg-chart-1/15 text-chart-1">{c.whales} whales</Badge>
                {c.trusted > 0 && <Badge className="bg-positive/15 text-positive">{c.trusted} trusted</Badge>}
                {c.mcap_usd != null && <span className="num text-xs text-muted-foreground">mcap {usd(c.mcap_usd)}</span>}
                <span className="num ml-auto text-xs text-muted-foreground">first buy {ago(c.first_ts)}</span>
              </li>
            ))}
          </ul>
        )}
      </Section>
      <Section title="Live whale trades" description={`Every pump.fun trade by the ${data?.whales_watched ?? "…"} whales in the book, about a second after it happens. Price change is since their trade.`}>
        {data?.error ? <Empty>Whale tape unavailable: {data.error}</Empty> : trades.length === 0 ? <Empty>No whale trades in the last hour yet.</Empty> : (
          <ul className="divide-y">
            {trades.slice(0, 60).map(t => (
              <li key={t.ts + t.wallet + t.mint + t.side} className="flex flex-wrap items-center gap-2 py-1 text-sm">
                <Badge className={t.side === "buy" ? "bg-positive/15 text-positive" : "bg-negative/15 text-negative"}>{t.side}</Badge>
                <CoinLink mint={t.mint}>{t.symbol}</CoinLink>
                {t.sol != null && <span className="num text-xs text-muted-foreground">{t.sol.toFixed(2)} SOL</span>}
                {t.x_since != null && <span className={cn("num text-xs", t.x_since >= 1 ? "text-positive" : "text-negative")}>{t.x_since >= 1 ? "+" : ""}{Math.round((t.x_since - 1) * 100)}% since</span>}
                <a href={`https://gmgn.ai/sol/address/${t.wallet}`} target="_blank" rel="noreferrer" className="font-mono text-xs text-muted-foreground hover:underline">{t.wallet.slice(0, 4)}…{t.wallet.slice(-4)}</a>
                {t.whale?.council && <Badge className={cn("text-[11px]", COUNCIL[t.whale.council] ?? "bg-muted")}>{t.whale.council}</Badge>}
                {t.whale?.win != null && <span className="num text-xs text-muted-foreground">win {Math.round(t.whale.win * 100)}%</span>}
                <span className="num ml-auto text-xs text-muted-foreground">{ago(t.ts)}</span>
              </li>
            ))}
          </ul>
        )}
      </Section>
    </div>
  )
}
