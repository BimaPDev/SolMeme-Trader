import { CircleCheck, CircleDashed, Loader2, MessageCircleQuestionMark, ShieldAlert, XCircle } from "lucide-react"
import { useState, type FormEvent } from "react"

import { Empty, Section } from "@/components/app/bits"
import { CoinLink } from "@/components/app/narratives"
import { Badge } from "@/components/ui/badge"
import { usePoll } from "@/lib/api"
import { ago, usd } from "@/lib/format"
import { cn } from "@/lib/utils"

type Vote = { judge: string; verdict?: string; confidence?: number; reasons?: string[]; risks?: string[]; plan?: string; error?: string; seconds?: number }
type Strat = { prefilter?: string | null; key?: string; role?: string; would_buy?: boolean; failed?: string[] }
type WalletFacts = {
  name?: string; tracked?: boolean; ban?: { role: string; reason: string } | null; linked_banned?: string[] | null
  whale?: { status?: string; score?: number; win?: number; median_exit?: number; round_trips?: number; council?: string }
  stats?: { winrate?: number; pnl?: number; buy_count?: number; error?: string }; heuristic?: [string, string[]] | null
  sightings?: { symbol: string; mint: string; x_since_buy?: number | null; sold?: boolean }[]
  coins?: Coins | null
}
type Coins = { summary: string; own: number; banned_creator: number; flagged: number; dead: number; ran: number
  rows: { mint: string; symbol: string; line: string; own: boolean; banned_creator: boolean; dead: boolean }[] }

function CoinForensics({ c }: { c?: Coins | null }) {
  if (!c || !c.rows?.length) return null
  const bad = c.own + c.banned_creator > 0 || c.dead > c.rows.length / 2
  return (
    <div className="mt-2 rounded-md bg-muted/40 p-2 text-xs">
      <div className="text-muted-foreground">Forensics on the coins it bought</div>
      <div className={cn("mt-0.5", bad ? "text-negative" : "")}>{c.summary}</div>
      <ul className="mt-1 space-y-0.5">
        {c.rows.map(r => (
          <li key={r.mint} className="flex flex-wrap gap-1.5"><CoinLink mint={r.mint}>{r.symbol}</CoinLink>
            <span className={cn("text-muted-foreground", (r.own || r.banned_creator || r.dead) && "text-negative")}>{r.line}</span></li>
        ))}
      </ul>
    </div>
  )
}
export type Ask = {
  id: string; kind?: "token" | "wallet" | "review" | "pick"; action?: "sold" | "held" | "bought" | "passed" | "skipped"; wallet?: WalletFacts; auto_added?: boolean; auto_error?: string; recheck?: boolean; why_asked?: string
  mint: string; note?: string; ts: number; done_ts?: number; status: "gathering" | "asking" | "done"
  symbol?: string; verdict?: string | null; agreement?: number; score?: Record<string, number>; votes?: Vote[]
  red_flags?: string[]; green_flags?: string[]; strategies?: Strat[]; mcap_usd?: number | null; evidence?: string; error?: string
}

const VERDICT: Record<string, { label: string; tone: string; icon: typeof CircleCheck }> = {
  buy: { label: "Buy", tone: "bg-positive/15 text-positive", icon: CircleCheck },
  wait: { label: "Wait", tone: "bg-chart-2/15 text-chart-2", icon: CircleDashed },
  avoid: { label: "Avoid", tone: "bg-negative/15 text-negative", icon: XCircle },
  track: { label: "Track", tone: "bg-positive/15 text-positive", icon: CircleCheck },
  hold: { label: "Hold", tone: "bg-positive/15 text-positive", icon: CircleCheck },
  sell: { label: "Sell now", tone: "bg-negative/15 text-negative", icon: XCircle },
  watch: { label: "Watch", tone: "bg-chart-2/15 text-chart-2", icon: CircleDashed },
  unknown: { label: "No answer", tone: "bg-muted text-muted-foreground", icon: ShieldAlert },
  skipped: { label: "Not asked", tone: "bg-muted text-muted-foreground", icon: CircleDashed },
}

/** A pasted DexScreener / pump.fun / GMGN link or a bare address: the token address in it. */
const mintOf = (s: string) => (s.match(/[1-9A-HJ-NP-Za-km-z]{32,44}/g) ?? []).pop() ?? ""

export function AskPage() {
  const asks = usePoll<{ asks: Ask[] }>("api/asks", 3000)
  const mine = usePoll<{ wallets: Record<string, { ts: number; note?: string; verdict?: string; by?: string; checked_ts?: number }>; max?: number
    removed?: { address: string; ts: number; reason: string; by: string }[] }>("api/my-wallets", 10000)
  const [kind, setKind] = useState<"token" | "wallet">("token")
  const [text, setText] = useState("")
  const [note, setNote] = useState("")
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState("")
  const mint = mintOf(text)

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!mint) { setErr(kind === "wallet" ? "Paste a wallet address or a GMGN / Solscan wallet link." : "Paste a token address or a DexScreener / pump.fun link."); return }
    setBusy(true); setErr("")
    try {
      const r = await fetch("api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mint, note, kind }) })
      const j = await r.json()
      if (!r.ok || j.error) setErr(j.error ?? `HTTP ${r.status}`)
      else { setText(""); setNote(""); asks.refresh() }
    } catch { setErr("Can't reach the bot") } finally { setBusy(false) }
  }

  const list = asks.data?.asks ?? []
  const tracked = mine.data?.wallets ?? {}
  async function track(address: string, action: "add" | "remove", verdict = "") {
    await fetch("api/my-wallets", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, address, verdict }) })
    mine.refresh()
  }
  return (
    <div className="space-y-4">
      <Section title="Ask the council" description={kind === "token"
        ? "Found a coin yourself? Paste it here. The bot follows the coin, gathers what it knows (holders, dev, red flags, which strategies would buy it), and the judges vote buy, wait or avoid. Advice only: nothing is bought or sold."
        : "Found a wallet worth following? Paste it here. The bot pulls its record (our whale book, GMGN 7-day stats, ban list, money links to banned wallets, coins we saw it buy) and the judges vote track, watch or avoid. You decide whether to track it."}>
        <div className="mb-2 inline-flex rounded-md border p-0.5 text-sm">
          {(["token", "wallet"] as const).map(k => (
            <button key={k} type="button" onClick={() => setKind(k)}
                    className={cn("rounded px-3 py-1", kind === k ? "bg-foreground text-background" : "text-muted-foreground hover:text-foreground")}>
              {k === "token" ? "Coin" : "Wallet"}
            </button>
          ))}
        </div>
        <form onSubmit={submit} className="space-y-2">
          <input value={text} onChange={e => setText(e.target.value)} placeholder={kind === "token" ? "Token address, or a DexScreener / pump.fun / GMGN link" : "Wallet address, or a GMGN / Solscan wallet link"}
                 className="h-10 w-full rounded-md border bg-background px-3 font-mono text-sm outline-none focus:ring-2 focus:ring-ring" />
          <input value={note} onChange={e => setNote(e.target.value)} placeholder="Optional: why you like it (the judges see this)" maxLength={200}
                 className="h-9 w-full rounded-md border bg-background px-3 text-sm outline-none focus:ring-2 focus:ring-ring" />
          <div className="flex flex-wrap items-center gap-3">
            <button type="submit" disabled={busy || !mint}
                    className="inline-flex h-9 items-center gap-2 rounded-md bg-foreground px-4 text-sm font-medium text-background disabled:opacity-50">
              {busy ? <Loader2 className="size-4 animate-spin" /> : <MessageCircleQuestionMark className="size-4" />} Ask the council
            </button>
            {mint && <span className="font-mono text-xs text-muted-foreground">{mint.slice(0, 6)}…{mint.slice(-4)}</span>}
            {err && <span className="text-sm text-negative">{err}</span>}
          </div>
          <p className="text-xs text-muted-foreground">Takes about a minute: the bot gathers data first, then the judges answer.</p>
        </form>
      </Section>
      <Section title="Wallets you track" description={`Ask about a wallet and the council decides: a Track verdict adds it here. Their buys count as whale buys (bigger bets) and the bot polls them live. Every 4 hours each one is checked again; a ban, an Avoid or two Watch in a row removes it. Up to ${mine.data?.max ?? 12}.`}>
        {Object.keys(tracked).length === 0 ? <Empty>None yet. Ask the council about a wallet (Wallet tab above).</Empty> : (
          <ul className="divide-y">
            {Object.entries(tracked).map(([w, t]) => <TrackedRow key={w} w={w} t={t} onRemove={() => track(w, "remove")} />)}
          </ul>
        )}
        {(mine.data?.removed ?? []).length > 0 && (
          <div className="mt-3 border-t pt-2">
            <div className="text-xs text-muted-foreground">Recently removed</div>
            <ul className="mt-1 space-y-0.5 text-xs">
              {mine.data!.removed!.map(r => (
                <li key={r.address + r.ts} className="flex flex-wrap gap-2"><span className="font-mono">{r.address.slice(0, 6)}…{r.address.slice(-4)}</span>
                  <span className="text-muted-foreground">{r.reason} · {ago(r.ts)}</span></li>
              ))}
            </ul>
          </div>
        )}
      </Section>
      <Section title="Your asks" description="Newest first. The bot keeps following each coin for 30 minutes.">
        {list.length === 0 ? <Empty>Nothing asked yet.</Empty> : <ul className="space-y-3">{list.map(a => a.kind === "wallet"
          ? <WalletCard key={a.id} a={a} tracked={a.mint in tracked} onTrack={(add, v) => track(a.mint, add ? "add" : "remove", v)} />
          : <AskCard key={a.id} a={a} />)}</ul>}
      </Section>
    </div>
  )
}

function AskCard({ a }: { a: Ask }) {
  const [open, setOpen] = useState(false)
  const v = VERDICT[a.verdict ?? ""] ?? null
  const strats = (a.strategies ?? []).filter(s => s.key)
  const filter = (a.strategies ?? [])[0]?.prefilter
  const buyers = strats.filter(s => s.would_buy)
  return (
    <li className="rounded-lg border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <CoinLink mint={a.mint}>{a.symbol || `${a.mint.slice(0, 6)}…`}</CoinLink>
        {a.status !== "done" ? (
          <Badge className="bg-muted text-muted-foreground"><Loader2 className="mr-1 size-3 animate-spin" />{a.status === "gathering" ? "gathering data" : "judges thinking"}</Badge>
        ) : v && (
          <Badge className={v.tone}><v.icon className="mr-1 size-3" />{v.label}{a.agreement != null && ` · ${Math.round(a.agreement * 100)}% agree`}</Badge>
        )}
        {a.kind === "review" && <Badge className="bg-chart-1/15 text-chart-1">live buy review{a.action === "sold" ? " · sold it" : ""}</Badge>}
        {a.kind === "pick" && <Badge className={a.action === "bought" ? "bg-positive/15 text-positive" : "bg-muted text-muted-foreground"}>
          council auto-pick · {a.action === "bought" ? "bought it" : a.action === "skipped" ? "skipped (filter)" : "passed"}</Badge>}
        {a.mcap_usd != null && <span className="num text-xs text-muted-foreground">mcap {usd(a.mcap_usd)}</span>}
        <span className="num ml-auto text-xs text-muted-foreground">{ago(a.ts)}</span>
      </div>
      {a.note && <p className="mt-1 text-xs italic text-muted-foreground">“{a.note}”</p>}
      {a.error && <p className="mt-2 text-sm text-negative">{a.error}</p>}
      {(a.red_flags?.length || a.green_flags?.length || strats.length > 0) ? (
        <div className="mt-2 grid gap-2 text-xs sm:grid-cols-3">
          <div className="rounded-md bg-muted/40 p-2"><div className="text-muted-foreground">Red flags</div>
            {a.red_flags?.length ? <ul className="mt-0.5 list-disc pl-4 text-negative">{a.red_flags.map(f => <li key={f}>{f}</li>)}</ul> : <div className="mt-0.5">none found</div>}</div>
          <div className="rounded-md bg-muted/40 p-2"><div className="text-muted-foreground">Green flags</div>
            {a.green_flags?.length ? <ul className="mt-0.5 list-disc pl-4 text-positive">{a.green_flags.map(f => <li key={f}>{f}</li>)}</ul> : <div className="mt-0.5">none found</div>}</div>
          <div className="rounded-md bg-muted/40 p-2"><div className="text-muted-foreground">The bot's strategies</div>
            <div className="mt-0.5">{filter ? <span className="text-negative">global filter blocks it ({filter})</span>
              : buyers.length ? <span className="text-positive">{buyers.length} would buy now: {buyers.map(b => b.key!.split("@")[0]).join(", ")}</span>
              : strats.length ? "none would buy right now" : "—"}</div></div>
        </div>
      ) : null}
      {(a.votes ?? []).length > 0 && (
        <ul className="mt-2 space-y-1.5">
          {a.votes!.map(vote => {
            const vv = VERDICT[vote.verdict ?? "unknown"] ?? VERDICT.unknown
            return (
              <li key={vote.judge} className="rounded-md border px-2.5 py-1.5 text-xs">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{vote.judge}</span>
                  {vote.error ? <span className="text-negative">couldn't answer: {vote.error}</span>
                    : <Badge className={cn(vv.tone, "text-[11px]")}>{vv.label} · {Math.round((vote.confidence ?? 0) * 100)}%</Badge>}
                  {vote.seconds != null && <span className="ml-auto text-muted-foreground">{vote.seconds}s</span>}
                </div>
                {vote.reasons?.length ? <div className="mt-1">{vote.reasons.join(" · ")}</div> : null}
                {vote.risks?.length ? <div className="mt-0.5 text-negative/90">Risks: {vote.risks.join(" · ")}</div> : null}
                {vote.plan ? <div className="mt-0.5 text-muted-foreground">Plan: {vote.plan}</div> : null}
              </li>
            )
          })}
        </ul>
      )}
      {a.evidence && (
        <div className="mt-2">
          <button type="button" onClick={() => setOpen(o => !o)} className="text-xs text-muted-foreground hover:underline">{open ? "hide" : "show"} what the judges saw</button>
          {open && <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap rounded-md bg-muted/40 p-2 text-[11px]">{a.evidence}</pre>}
        </div>
      )}
    </li>
  )
}

function Votes({ votes }: { votes: Vote[] }) {
  return (
    <ul className="mt-2 space-y-1.5">
      {votes.map(vote => {
        const vv = VERDICT[vote.verdict ?? "unknown"] ?? VERDICT.unknown
        return (
          <li key={vote.judge} className="rounded-md border px-2.5 py-1.5 text-xs">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{vote.judge}</span>
              {vote.error ? <span className="text-negative">couldn't answer: {vote.error}</span>
                : <Badge className={cn(vv.tone, "text-[11px]")}>{vv.label} · {Math.round((vote.confidence ?? 0) * 100)}%</Badge>}
              {vote.seconds != null && <span className="ml-auto text-muted-foreground">{vote.seconds}s</span>}
            </div>
            {vote.reasons?.length ? <div className="mt-1">{vote.reasons.join(" · ")}</div> : null}
            {vote.risks?.length ? <div className="mt-0.5 text-negative/90">Risks: {vote.risks.join(" · ")}</div> : null}
          </li>
        )
      })}
    </ul>
  )
}

function WalletCard({ a, tracked, onTrack }: { a: Ask; tracked: boolean; onTrack: (add: boolean, verdict: string) => void }) {
  const [open, setOpen] = useState(false)
  const v = VERDICT[a.verdict ?? ""] ?? null
  const w = a.wallet ?? {}
  const st = w.stats ?? {}
  const seen = w.sightings ?? []
  const up = seen.filter(s => (s.x_since_buy ?? 0) > 1).length
  return (
    <li className="rounded-lg border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <a href={`https://gmgn.ai/sol/address/${a.mint}`} target="_blank" rel="noreferrer" className="font-semibold hover:underline">
          {w.name || `${a.mint.slice(0, 6)}…${a.mint.slice(-4)}`}</a>
        <Badge className="bg-muted text-muted-foreground">wallet</Badge>
        {a.status !== "done" ? (
          <Badge className="bg-muted text-muted-foreground"><Loader2 className="mr-1 size-3 animate-spin" />{a.status === "gathering" ? "gathering data" : "judges thinking"}</Badge>
        ) : v && <Badge className={v.tone}><v.icon className="mr-1 size-3" />{v.label}{a.agreement != null && ` · ${Math.round(a.agreement * 100)}% agree`}</Badge>}
        <span className="num ml-auto text-xs text-muted-foreground">{ago(a.ts)}</span>
      </div>
      {a.error && <p className="mt-2 text-sm text-negative">{a.error}</p>}
      {a.status !== "gathering" && (
        <div className="num mt-2 grid gap-2 text-xs sm:grid-cols-3">
          <div className="rounded-md bg-muted/40 p-2"><div className="text-muted-foreground">GMGN, last 7 days</div>
            <div className="mt-0.5">{st.error ? "unavailable" : <>win {st.winrate != null ? `${Math.round(st.winrate * 100)}%` : "—"} · pnl {st.pnl != null ? `${st.pnl.toFixed(2)}x` : "—"} · {st.buy_count ?? "—"} buys</>}</div></div>
          <div className="rounded-md bg-muted/40 p-2"><div className="text-muted-foreground">Our whale book</div>
            <div className="mt-0.5">{w.whale?.status ? <>{w.whale.status} · {w.whale.round_trips ?? 0} round trips · win {w.whale.win != null ? `${Math.round(w.whale.win * 100)}%` : "—"}{w.whale.council ? ` · council: ${w.whale.council}` : ""}</> : "not scored yet"}</div></div>
          <div className="rounded-md bg-muted/40 p-2"><div className="text-muted-foreground">Red flags</div>
            <div className={cn("mt-0.5", (w.ban || w.linked_banned?.length) && "text-negative")}>
              {w.ban ? `banned (${w.ban.role})` : w.linked_banned?.length ? `money links to ${w.linked_banned.length} banned wallet(s)` : w.linked_banned == null ? "chain check unavailable" : "none found"}</div></div>
        </div>
      )}
      <CoinForensics c={w.coins} />
      {seen.length > 0 && <p className="mt-2 text-xs text-muted-foreground">The bot saw it buy {seen.length} coin(s); {up} are above its buy price now: {seen.slice(0, 6).map(s => `${s.symbol}${s.x_since_buy ? ` ${s.x_since_buy}x` : ""}`).join(", ")}</p>}
      {(a.votes ?? []).length > 0 && <Votes votes={a.votes!} />}
      {a.status === "done" && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {tracked ? <button type="button" onClick={() => onTrack(false, "")} className="rounded-md border px-3 py-1 text-xs hover:bg-muted">Stop tracking</button>
            : <button type="button" onClick={() => onTrack(true, a.verdict ?? "")}
                      className={cn("rounded-md px-3 py-1 text-xs font-medium", a.verdict === "avoid" ? "border text-muted-foreground hover:bg-muted" : "bg-foreground text-background")}>
                Track this wallet{a.verdict === "avoid" ? " anyway" : ""}</button>}
          {tracked && <span className="text-xs text-positive">{a.auto_added ? "the council added it to your tracked wallets" : "tracked: its buys count as whale buys"}</span>}
          {a.auto_error && <span className="text-xs text-negative">couldn't add: {a.auto_error}</span>}
          {a.recheck && <span className="text-xs text-muted-foreground">4-hour re-check ({a.why_asked})</span>}
        </div>
      )}
      {a.evidence && (
        <div className="mt-2">
          <button type="button" onClick={() => setOpen(o => !o)} className="text-xs text-muted-foreground hover:underline">{open ? "hide" : "show"} what the judges saw</button>
          {open && <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap rounded-md bg-muted/40 p-2 text-[11px]">{a.evidence}</pre>}
        </div>
      )}
    </li>
  )
}

type Detail = {
  why?: string[]; verdict?: string; error?: string; last_ask_ts?: number; coins?: Coins | null
  trades?: { ts: number; mint: string; symbol: string; side: string; sol?: number | null; x_since?: number | null; source?: string }[]
  seen?: { symbol: string; mint: string; x_since_buy?: number | null; sold?: boolean; bought_ago_min?: number }[]
  gmgn_recent?: { side?: string; symbol?: string; usd?: number | string }[]
}

function TrackedRow({ w, t, onRemove }: { w: string; t: { ts: number; verdict?: string; by?: string; checked_ts?: number; name?: string }; onRemove: () => void }) {
  const [open, setOpen] = useState(false)
  const [d, setD] = useState<Detail | null>(null)
  async function toggle() {
    const next = !open
    setOpen(next)
    if (next) {
      try { setD(await (await fetch(`api/my-wallets?detail=${w}`, { cache: "no-store" })).json()) } catch { setD({ error: "Can't reach the bot" }) }
    }
  }
  const trades = d?.trades ?? []
  const seen = (d?.seen ?? []).filter(s => !trades.some(x => x.mint === s.mint))
  return (
    <li className="py-1.5 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" onClick={toggle} className="font-mono hover:underline">{open ? "▾" : "▸"} {t.name || `${w.slice(0, 6)}…${w.slice(-4)}`}</button>
        {t.verdict && <Badge className={(VERDICT[t.verdict] ?? VERDICT.unknown).tone}>council: {t.verdict}</Badge>}
        <span className="num text-xs text-muted-foreground">{t.by === "council" ? "added by the council" : "added by you"} {ago(t.ts)}{t.checked_ts ? ` · checked ${ago(t.checked_ts)}` : ""}</span>
        <button type="button" onClick={onRemove} className="ml-auto rounded-md border px-2 py-0.5 text-xs hover:bg-muted">stop tracking</button>
      </div>
      {open && (
        <div className="mt-2 space-y-2 rounded-md bg-muted/30 p-2.5 text-xs">
          {!d ? <div className="text-muted-foreground"><Loader2 className="mr-1 inline size-3 animate-spin" />loading…</div> : d.error ? <div className="text-negative">{d.error}</div> : (
            <>
              <div>
                <div className="font-medium">Why it's tracked</div>
                {d.why?.length ? <ul className="mt-0.5 list-disc pl-4">{d.why.map(r => <li key={r}>{r}</li>)}</ul>
                  : <div className="text-muted-foreground">No council reasons saved{t.by !== "council" ? " (you added it yourself)" : ""}. Ask about it again to get them.</div>}
              </div>
              <div>
                <div className="font-medium">Coins it traded <span className="font-normal text-muted-foreground">(recorded since it was tracked; price change since its trade)</span></div>
                {trades.length === 0 && seen.length === 0 ? <div className="text-muted-foreground">No trades recorded yet. They show up as the bot sees them.</div> : (
                  <ul className="mt-1 divide-y">
                    {trades.map(x => (
                      <li key={x.ts + x.mint + x.side} className="flex flex-wrap items-center gap-2 py-1">
                        <Badge className={x.side === "buy" ? "bg-positive/15 text-positive" : "bg-negative/15 text-negative"}>{x.side}</Badge>
                        <CoinLink mint={x.mint}>{x.symbol}</CoinLink>
                        {x.sol != null && <span className="num text-muted-foreground">{x.sol.toFixed(3)} SOL</span>}
                        {x.x_since != null && <span className={cn("num", x.x_since >= 1 ? "text-positive" : "text-negative")}>{x.x_since >= 1 ? "+" : ""}{Math.round((x.x_since - 1) * 100)}% since</span>}
                        <span className="num ml-auto text-muted-foreground">{ago(x.ts)}</span>
                      </li>
                    ))}
                    {seen.map(x => (
                      <li key={x.mint} className="flex flex-wrap items-center gap-2 py-1">
                        <Badge className="bg-muted text-muted-foreground">{x.sold ? "bought + sold" : "bought"}</Badge>
                        <CoinLink mint={x.mint}>{x.symbol}</CoinLink>
                        {x.x_since_buy != null && <span className={cn("num", x.x_since_buy >= 1 ? "text-positive" : "text-negative")}>{x.x_since_buy >= 1 ? "+" : ""}{Math.round((x.x_since_buy - 1) * 100)}% since</span>}
                        {x.bought_ago_min != null && <span className="num ml-auto text-muted-foreground">{x.bought_ago_min}m ago</span>}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
              <CoinForensics c={d.coins} />
              {(d.gmgn_recent ?? []).length > 0 && (
                <div>
                  <div className="font-medium">Recent activity on GMGN <span className="font-normal text-muted-foreground">(when last asked)</span></div>
                  <div className="mt-0.5 text-muted-foreground">{d.gmgn_recent!.map(g => `${g.side ?? ""} ${g.symbol ?? "?"}${g.usd ? ` $${Math.round(Number(g.usd))}` : ""}`).join(" · ")}</div>
                </div>
              )}
              <a href={`https://gmgn.ai/sol/address/${w}`} target="_blank" rel="noreferrer" className="inline-block text-muted-foreground hover:underline">open on GMGN ↗</a>
            </>
          )}
        </div>
      )}
    </li>
  )
}
