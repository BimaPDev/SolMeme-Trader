import { useEffect, useState, type ReactNode } from "react"
import { Copy, ExternalLink, Flame, ShieldAlert, ShieldCheck } from "lucide-react"

import { Empty, Section, SortHead, Stat, useSort } from "@/components/app/bits"
import { Badge } from "@/components/ui/badge"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { usePoll, type Headline, type Narratives, type TokenCard } from "@/lib/api"
import { useInspect } from "@/lib/inspect"
import { ago, int, num, pct, usd } from "@/lib/format"
import { cn } from "@/lib/utils"

/** Headlines and tweets are untrusted: only plain https links become clickable. */
const safeUrl = (u: string | null | undefined) => (u && /^https:\/\//i.test(u) ? u : null)

const followers = (n: number | null | undefined) =>
  n == null ? "" : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(Math.round(n))

const SOURCE_LABEL: Record<string, string> = { gnews: "Google News", gtrends: "Google Trends", reddit: "Reddit", j7tracker: "X", j7ai: "j7 AI" }

/** Copy a coin or wallet address; the button says "copied" for a moment. */
function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false)
  return (
    <button type="button" aria-label="Copy address" title="Copy address"
      onClick={() => { navigator.clipboard?.writeText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1500) }, () => {}) }}
      className="inline-flex shrink-0 items-center gap-1 rounded border px-1.5 py-0.5 font-sans text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground">
      <Copy className="size-3" />{done ? "copied" : "copy"}
    </button>
  )
}

function Ext({ href, children, className }: { href: string | null; children: ReactNode; className?: string }) {
  return href ? <a className={cn("hover:underline", className)} href={href} target="_blank" rel="noreferrer noopener">{children}</a>
    : <span className={className}>{children}</span>
}

export function NarrativesView() {
  const { data } = usePoll<Narratives>("api/narratives", 15000)
  const themes = data?.themes ?? []
  const { sorted: rows, sort, toggle } = useSort(themes, {
    launches: t => t.launches, ran: t => t.ran_2x, hit: t => t.hit_pct, tweets: t => t.tweets, news: t => t.news,
  }, "themes")
  const now = data?.now ?? Date.now() / 1000
  const c = data?.counts ?? {}
  return (
    <div className="grid gap-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Tweets" value={int(c.j7tracker ?? 0)} sub="j7tracker, kept for matching" />
        <Stat label="AI coin guesses" value={int(c.j7ai ?? 0)} sub="j7's guess per tweet" />
        <Stat label="Headlines" value={int((c.gnews ?? 0) + (c.gtrends ?? 0) + (c.reddit ?? 0))} sub="News, Trends, Reddit" />
        <Stat label="Hot themes" value={int(themes.length)} sub="3+ launches in the last hour" />
      </div>

      <Section title={<span className="flex items-center gap-2"><Flame className="size-4" />Hot themes</span>}
        description="Words in launch names over the last hour: how crowded each theme is, how many of its coins ran 2x, and whether tweets or news are behind it. The swarm tests these as rules (tweet_match_30m, ai_ticker_match, news_match_6h, theme_launches_1h).">
        {rows.length === 0 ? <Empty>No theme has 3+ launches yet. Themes appear as launches come in.</Empty> : (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Theme</TableHead>
                  <SortHead k="launches" sort={sort} toggle={toggle} right>Launches</SortHead>
                  <SortHead k="ran" sort={sort} toggle={toggle} right>Ran 2x</SortHead>
                  <SortHead k="hit" sort={sort} toggle={toggle} right>Hit</SortHead>
                  <SortHead k="tweets" sort={sort} toggle={toggle} right>Tweets</SortHead>
                  <SortHead k="news" sort={sort} toggle={toggle} right>News</SortHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map(t => (
                  <TableRow key={t.word}>
                    <TableCell className="font-medium">{t.word}</TableCell>
                    <TableCell className="num text-right">{t.launches}</TableCell>
                    <TableCell className="num text-right">{t.ran_2x}</TableCell>
                    <TableCell className={cn("num text-right", t.hit_pct >= 20 ? "text-positive" : t.hit_pct === 0 ? "text-muted-foreground" : "")}>{t.hit_pct}%</TableCell>
                    <TableCell className="num text-right">{t.tweets ? <span title={`largest account ${followers(t.max_followers)}`}>{t.tweets}</span> : "—"}</TableCell>
                    <TableCell className="num text-right">{t.news || "—"}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </Section>

      <Section title={<span className="flex items-center gap-2"><Copy className="size-4" />Copycat clusters</span>}
        description="When a coin takes off, copies with the same ticker or name launch within minutes. The first one is the original (OG); it reached $30k about 5x as often as its copies in our data. The og_hunter trial and the swarm test is_og / copies_after.">
        {(data?.clusters ?? []).length === 0 ? <Empty>No ticker has 2+ copies in the last 2 hours yet.</Empty> : (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Ticker / name</TableHead>
                  <TableHead>Original</TableHead>
                  <TableHead className="text-right">Copies</TableHead>
                  <TableHead className="text-right">OG since launch</TableHead>
                  <TableHead className="text-right">Last copy</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(data?.clusters ?? []).map(c => (
                  <TableRow key={c.og + c.key}>
                    <TableCell className="font-mono text-xs">{c.key}</TableCell>
                    <TableCell><CoinLink mint={c.og}>{c.symbol}</CoinLink> <span className="text-xs text-muted-foreground">{ago(c.og_ts, now)}</span></TableCell>
                    <TableCell className="num text-right">{c.copies}</TableCell>
                    <TableCell className={cn("num text-right", c.og_x == null ? "text-muted-foreground" : c.og_x >= 1 ? "text-positive" : "text-negative")}>{c.og_x == null ? "not priced" : `${num(c.og_x, 2)}x`}</TableCell>
                    <TableCell className="num text-right text-muted-foreground">{ago(c.last_copy_ts, now)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="AI coin guesses" description="For each tweet, j7tracker's AI guesses the coin it will spawn. A launch with that ticker counts as ai_ticker_match.">
          <Feed items={data?.ai ?? []} now={now} render={h => (
            <><Badge variant="secondary" className="font-mono">${h.ticker || "?"}</Badge> <span>{h.name}</span></>
          )} />
        </Section>
        <Section title="Tweets" description="Tracked X accounts via j7tracker. Shown as posted; never acted on directly.">
          <Feed items={data?.tweets ?? []} now={now} render={h => (
            <><span className="font-medium">@{h.author}</span>{h.followers ? <span className="text-muted-foreground"> · {followers(h.followers)}</span> : null}
              <div className="mt-0.5 whitespace-pre-line text-muted-foreground">{h.text}</div></>
          )} />
        </Section>
      </div>

      <Section title="News & trends" description="Google News, Google Trends and Reddit's front page. Coins named after these count as news_match_6h.">
        <Feed items={data?.news ?? []} now={now} render={h => (
          <><Badge variant="outline">{SOURCE_LABEL[h.source] ?? h.source}</Badge> <span>{h.text}</span></>
        )} />
      </Section>
    </div>
  )
}

function Feed({ items, now, render }: { items: Headline[]; now: number; render: (h: Headline) => ReactNode }) {
  if (!items.length) return <Empty>Nothing yet.</Empty>
  return (
    <ul className="max-h-[28rem] space-y-2 overflow-y-auto pr-1 text-sm">
      {items.map((h, i) => (
        <li key={`${h.source}${h.ts}${i}`} className="rounded-md border p-2 [overflow-wrap:anywhere]">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">{render(h)}</div>
            <div className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground">
              {ago(h.ts, now)}
              {safeUrl(h.url) && <Ext href={safeUrl(h.url)}><ExternalLink className="size-3" /></Ext>}
            </div>
          </div>
        </li>
      ))}
    </ul>
  )
}

/** A coin name that opens its forensics card. */
export function CoinLink({ mint, children, className }: { mint: string; children: ReactNode; className?: string }) {
  const inspect = useInspect()
  return <button type="button" className={cn("text-left font-medium hover:underline", className)} onClick={() => inspect.coin(mint)}>{children}</button>
}

const FEATURE_LABELS: [string, string, (v: number) => string][] = [
  ["mcap_usd", "Market cap", v => usd(v, 0)],
  ["age_s", "Age", v => `${Math.round(v / 60)}m`],
  ["gmgn_holders", "Holders", v => int(v)],
  ["gmgn_top10_pct", "Top 10 hold", v => pct(v)],
  ["gmgn_bundler_rate", "Bundlers", v => pct(v * 100)],
  ["gmgn_insider_rate", "Insiders", v => pct(v * 100)],
  ["gmgn_dev_hold_pct", "Dev holds", v => pct(v)],
  ["gmgn_sniper_count", "Snipers", v => int(v)],
  ["gmgn_rug_ratio", "Rug ratio", v => num(v, 2)],
  ["dex_buy_ratio_m5", "Buys / sells 5m", v => num(v, 2)],
  ["vol_mcap_1h", "1h vol / mcap", v => num(v, 2)],
  ["dd_from_ath_pct", "Off its high", v => pct(v)],
  ["kol_buys", "KOL buys", v => int(v)],
  ["theme_launches_1h", "Same-theme launches 1h", v => int(v)],
  ["og_rank", "Launch # of its ticker", v => int(v)],
  ["copies_after", "Copycats after it", v => int(v)],
]

export function CoinSheet({ mint, onClose }: { mint: string; onClose: () => void }) {
  const [d, setD] = useState<TokenCard | null>(null)
  useEffect(() => {
    let alive = true
    const load = () => fetch(`api/token?mint=${encodeURIComponent(mint)}`, { cache: "no-store" })
      .then(r => r.json()).then(x => { if (alive) setD(x) }).catch(() => {})
    load()
    const id = setInterval(load, 20000)
    return () => { alive = false; clearInterval(id) }
  }, [mint])
  const f = d?.features ?? {}
  return (
    <Sheet open onOpenChange={o => { if (!o) onClose() }}>
      <SheetContent className="overflow-y-auto data-[side=right]:w-full data-[side=right]:sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>{d?.symbol || mint.slice(0, 6)} {d?.name ? <span className="font-normal text-muted-foreground">· {d.name}</span> : null}</SheetTitle>
          <SheetDescription className="flex items-start gap-2 font-mono text-xs">
            <a href={`https://dexscreener.com/solana/${mint}`} target="_blank" rel="noreferrer" title="Open on DexScreener"
               className="[overflow-wrap:anywhere] underline decoration-muted-foreground/40 underline-offset-2 hover:text-foreground hover:decoration-foreground">
              {mint}<ExternalLink className="ml-1 inline size-3 align-[-1px]" />
            </a>
            <CopyButton text={mint} />
          </SheetDescription>
        </SheetHeader>
        <div className="space-y-4 px-4 pb-6 text-sm">
          {!d ? <Empty>Loading…</Empty> : d.error ? <Empty>{d.error}</Empty> : (
            <>
              <div className="flex flex-wrap gap-2 text-xs">
                {Object.entries(d.links ?? {}).map(([k, u]) => <Ext key={k} href={safeUrl(u)} className="rounded border px-2 py-1">{k}</Ext>)}
              </div>
              {(d.red_flags.length > 0 || d.green_flags.length > 0) && (
                <ul className="space-y-1">
                  {d.red_flags.map(x => <li key={x} className="flex gap-2 text-negative"><ShieldAlert className="size-4 shrink-0" />{x}</li>)}
                  {d.green_flags.map(x => <li key={x} className="flex gap-2 text-positive"><ShieldCheck className="size-4 shrink-0" />{x}</li>)}
                </ul>
              )}
              <div className="grid grid-cols-2 gap-x-4 gap-y-1">
                {FEATURE_LABELS.map(([k, label, fmt]) => (
                  <div key={k} className="flex justify-between gap-2 border-b py-1">
                    <span className="text-muted-foreground">{label}</span>
                    <span className="num">{typeof f[k] === "number" ? fmt(f[k] as number) : "—"}</span>
                  </div>
                ))}
              </div>
              {d.creator && (
                <div>
                  <div className="mb-1 font-medium">Creator</div>
                  <Ext href={`https://gmgn.ai/sol/address/${d.creator.wallet}`} className="font-mono text-xs">{d.creator.wallet}</Ext>
                  {d.creator.banned && <div className="text-negative">Banned: {d.creator.ban_reason || "on the ban list"}</div>}
                  {d.creator.whale && <div className="text-muted-foreground">Whale book: {d.creator.whale}</div>}
                </div>
              )}
              <div>
                <div className="mb-1 font-medium">Tracked buyers ({d.buyers.length})</div>
                {d.buyers.length === 0 ? <div className="text-muted-foreground">No tracked, KOL or whale wallet bought it.</div> : (
                  <ul className="space-y-1 text-xs">
                    {d.buyers.map(b => (
                      <li key={b.wallet} className="flex items-center justify-between gap-2">
                        <Ext href={`https://gmgn.ai/sol/address/${b.wallet}`} className="font-mono">{b.wallet.slice(0, 6)}…{b.wallet.slice(-4)}</Ext>
                        <span className="flex gap-1">
                          <Badge variant="outline">{b.label}</Badge>
                          {b.whale && <Badge className="bg-chart-3/15 text-chart-3">whale</Badge>}
                          {b.banned && <Badge className="bg-negative/15 text-negative">banned</Badge>}
                          {b.sold && <Badge variant="secondary">sold</Badge>}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
              <div>
                <div className="mb-1 font-medium">The story</div>
                <div className="mb-1 text-xs text-muted-foreground">Recent tweets and headlines sharing a word with its name: {d.words.join(", ") || "—"}</div>
                <Feed items={d.story} now={Date.now() / 1000} render={h => (
                  <><Badge variant="outline">{SOURCE_LABEL[h.source] ?? h.source}</Badge> {h.author ? <span className="font-medium">@{h.author} </span> : null}<span>{h.ticker ? `$${h.ticker} · ` : ""}{h.text}</span></>
                )} />
              </div>
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
