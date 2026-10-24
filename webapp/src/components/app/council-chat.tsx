import { Gavel, MessagesSquare } from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"

import { Empty, Section } from "@/components/app/bits"
import { Badge } from "@/components/ui/badge"
import { usePoll, type CouncilChatView, type CouncilMessage } from "@/lib/api"
import { ago, clock, dayClock } from "@/lib/format"
import { cn } from "@/lib/utils"

const PHASE: Record<CouncilMessage["phase"], string> = {
  open: "opening", explain: "explaining", check: "data check", vote: "voting", verdict: "verdict",
  bans: "ban review", close: "closed",
}

// one steady color per model family, so a judge is recognisable at a glance
const HUES: [RegExp, number][] = [
  [/sonnet|claude|opus|fable/, 35], [/gpt/, 150], [/gemini/, 230], [/grok/, 290], [/composer/, 190],
  [/qwen27/, 75], [/qwen8/, 110], [/9router/, 340],
]
function hue(name: string) {
  const hit = HUES.find(([re]) => re.test(name))
  if (hit) return hit[1]
  let h = 0
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) % 360
  return h
}

/** Each sitting of the council as a chat: who is thinking, what each judge said, and how the data and the vote went. */
export function CouncilChat() {
  const [pick, setPick] = useState("")                    // "" follows the newest sitting
  const [fast, setFast] = useState(true)
  const url = "api/council-chat" + (pick ? `?sitting=${encodeURIComponent(pick)}` : "")
  const { data, ok } = usePoll<CouncilChatView>(url, fast ? 3000 : 20000, 15000)
  const current = data?.sittings.find(s => s.sitting === data.sitting)
  const live = !!current?.live
  if ((live || !data) !== fast) setFast(live || !data)     // poll every 3s while a sitting is live, else every 20s

  const lines = useMemo(() => visible(data?.messages ?? []), [data])
  const box = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)                              // stick to the bottom unless the reader scrolled up
  useEffect(() => {
    const el = box.current
    if (el && pinned.current) el.scrollTop = el.scrollHeight
  }, [lines.length, data?.sitting])

  const gen = data?.generation
  const title = (
    <span className="flex items-center gap-2">
      <MessagesSquare className="size-4" /> Council chat
      {live && <Badge className="gap-1.5 bg-negative/15 text-negative"><span className="size-1.5 animate-pulse rounded-full bg-negative" />LIVE</Badge>}
    </span>
  )
  const description = !data ? "Loading…" : current
    ? `Generation ${current.gen ?? "?"} · started ${dayClock(current.started)} · ${live ? "in session" : `ended ${ago(current.last_ts, data.now)}`}`
      + (gen?.gen != null && gen.phase ? ` · swarm now: gen ${gen.gen}, ${gen.phase}` : "")
    : "The council meets during each swarm generation; its next sitting shows up here as it happens."
  const action = data && data.sittings.length > 1 && (
    <select value={pick} onChange={e => { setPick(e.target.value); pinned.current = true }}
            className="w-28 shrink-0 rounded-md border bg-background px-2 py-1 text-xs sm:w-auto sm:max-w-[13rem]">
      <option value="">Newest sitting</option>
      {data.sittings.map(s => (
        <option key={s.sitting} value={s.sitting}>
          {s.live ? "● " : ""}gen {s.gen ?? "?"} · {dayClock(s.started)}{s.closed ? "" : " (unfinished)"}
        </option>
      ))}
    </select>
  )

  return (
    <Section title={title} description={description} action={action}>
      {!ok && <p className="mb-2 text-xs text-negative">Can't reach the bot; showing the last messages received.</p>}
      {data?.error && <p className="mb-2 text-xs text-negative">{data.error}</p>}
      {!data || lines.length === 0 ? (
        <Empty>No sitting recorded yet. {gen?.gen != null ? `The swarm is on generation ${gen.gen} (${gen.phase ?? "starting"}); ` : ""}the council speaks during the generation's "council" step.</Empty>
      ) : (
        <div ref={box} onScroll={e => {
          const el = e.currentTarget
          pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
        }} className="max-h-[70vh] space-y-3 overflow-y-auto pr-1">
          {lines.map(m => <Line key={m.id} m={m} judges={data.judges} now={data.now} />)}
        </div>
      )}
    </Section>
  )
}

/** Hide "is thinking" once that judge has answered the same question; keep it while it is still pending. */
function visible(msgs: CouncilMessage[]) {
  const answered = new Set(msgs.filter(m => m.kind === "answer" || m.kind === "error")
    .map(m => `${m.speaker}|${m.topic}|${m.phase}`))
  return msgs.filter(m => m.kind !== "asking" || !answered.has(`${m.speaker}|${m.topic}|${m.phase}`))
}

function Line({ m, judges, now }: { m: CouncilMessage; judges: CouncilChatView["judges"]; now: number }) {
  if (m.speaker === "council") return <ChairLine m={m} />
  const h = hue(m.speaker)
  const model = m.data.model ?? judges[m.speaker]?.model ?? ""
  const backup = judges[m.speaker]?.fallback
  return (
    <div className="flex min-w-0 gap-2.5">
      <div className="flex size-8 shrink-0 items-center justify-center rounded-full text-xs font-semibold uppercase"
           style={{ backgroundColor: `oklch(0.72 0.14 ${h} / 0.2)`, color: `oklch(0.8 0.14 ${h})` }}>
        {m.speaker.replace(/^cursor-/, "").slice(0, 2)}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-xs">
          <span className="font-semibold" style={{ color: `oklch(0.8 0.14 ${h})` }}>{m.speaker}</span>
          {backup && <span className="text-muted-foreground">backup</span>}
          {model && <span className="font-mono text-muted-foreground">{model}</span>}
          <Topic m={m} />
          <span className="ml-auto text-muted-foreground" title={dayClock(m.ts)}>
            {clock(m.ts)}{m.data.seconds != null ? ` · ${Math.round(m.data.seconds)}s` : ""}
          </span>
        </div>
        {m.kind === "asking" ? (
          <div className="mt-1 inline-flex items-center gap-1.5 rounded-lg bg-muted/50 px-3 py-2 text-sm text-muted-foreground">
            thinking<Dots /> <span className="text-xs">({Math.round(now - m.ts)}s)</span>
          </div>
        ) : (
          <div className={cn("mt-1 rounded-lg px-3 py-2 text-sm", m.kind === "error" ? "bg-negative/10 text-negative" : "bg-muted/50")}>
            <Body text={m.text} />
            {m.data.raw != null && (
              <details className="mt-1.5 text-xs text-muted-foreground">
                <summary className="cursor-pointer select-none">raw answer</summary>
                <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded bg-background/60 p-2 font-mono [overflow-wrap:anywhere]">{JSON.stringify(m.data.raw, null, 2)}</pre>
              </details>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function ChairLine({ m }: { m: CouncilMessage }) {
  const strong = m.phase === "verdict" || m.phase === "open" || m.phase === "close"
  const tint = m.phase === "verdict" && m.data.approved != null
    ? (m.data.approved ? "border-positive/40 bg-positive/10" : "border-border bg-muted/30")
    : "border-dashed bg-transparent"
  return (
    <div className={cn("min-w-0 rounded-lg border px-3 py-2 text-sm", tint)}>
      <div className="flex flex-wrap items-baseline gap-x-2 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1 font-semibold text-foreground"><Gavel className="size-3.5" />council</span>
        <Topic m={m} />
        <span className="ml-auto" title={dayClock(m.ts)}>{clock(m.ts)}</span>
      </div>
      <div className={cn("mt-1", !strong && "text-muted-foreground")}><Body text={m.text} /></div>
      {m.data.evidence && (
        <details className="mt-1.5 text-xs text-muted-foreground">
          <summary className="cursor-pointer select-none">what the judges were shown</summary>
          <pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap rounded bg-background/60 p-2 font-mono [overflow-wrap:anywhere]">{m.data.evidence}</pre>
        </details>
      )}
    </div>
  )
}

function Topic({ m }: { m: CouncilMessage }) {
  return (
    <span className="rounded bg-muted px-1.5 py-px text-[11px] text-muted-foreground">
      {PHASE[m.phase] ?? m.phase}{m.topic && m.topic !== "bans" ? ` · ${m.topic}` : ""}
    </span>
  )
}

/** Plain text, one line per line; votes and checks get their verdict color. */
function Body({ text }: { text: string }) {
  return (
    <div className="space-y-0.5 [overflow-wrap:anywhere]">
      {text.split("\n").map((ln, i) => (
        <p key={i} className={cn(
          /^(✓|APPROVED|#\d+ (BAN|ban))/.test(ln) && "text-positive",
          /^(✗|Rejected)/.test(ln) && "text-negative",
          ln.startsWith("•") && "pl-2",
        )}>{ln}</p>
      ))}
    </div>
  )
}

function Dots() {
  return (
    <span className="inline-flex gap-0.5">
      {[0, 150, 300].map(d => <span key={d} className="size-1 animate-bounce rounded-full bg-current" style={{ animationDelay: `${d}ms` }} />)}
    </span>
  )
}
