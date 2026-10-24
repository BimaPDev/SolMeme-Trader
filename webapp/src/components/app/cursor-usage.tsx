import { ChevronDown, ExternalLink, Gauge } from "lucide-react"
import { useState } from "react"

import { Section } from "@/components/app/bits"
import { Progress } from "@/components/ui/progress"
import { usePoll, type CursorMeter, type CursorUsage } from "@/lib/api"
import { ago, dayClock, int } from "@/lib/format"
import { cn } from "@/lib/utils"

const USAGE_PAGE = "https://cursor.com/dashboard?tab=usage"

// The account summary is Cursor's internal dashboard JSON (undocumented), so every field is read defensively.
type Obj = Record<string, unknown>
const obj = (x: unknown): Obj | null => (x && typeof x === "object" ? (x as Obj) : null)
const n = (x: unknown): number | null => (typeof x === "number" && Number.isFinite(x) ? x : null)
const dollars = (cents: number | null) => (cents == null ? "—" : `$${(cents / 100).toFixed(2)}`)
const tokens = (x: number) => (x >= 1e6 ? `${(x / 1e6).toFixed(2)}M` : x >= 1e3 ? `${(x / 1e3).toFixed(1)}k` : int(x))

function plan(summary: Obj | undefined) {
  const indiv = obj(summary?.individualUsage)
  const p = obj(indiv?.plan)
  const od = obj(indiv?.onDemand)
  const used = n(p?.used), limit = n(p?.limit)
  const pctUsed = used != null && limit ? (100 * used) / limit : null      // the bar follows the dollars
  const end = typeof summary?.billingCycleEnd === "string" ? Date.parse(summary.billingCycleEnd as string) / 1000 : null
  return {
    used, limit, pctUsed, remaining: n(p?.remaining) ?? (used != null && limit != null ? Math.max(0, limit - used) : null),
    auto: n(p?.autoPercentUsed), api: n(p?.apiPercentUsed), total: n(p?.totalPercentUsed),
    onDemandUsed: n(od?.used), onDemandLimit: n(od?.limit),
    end: end && Number.isFinite(end) ? end : null,
    tier: typeof summary?.membershipType === "string" ? (summary.membershipType as string) : null,
    unlimited: summary?.isUnlimited === true,
  }
}

/** Cursor's two usage pools, as its own dashboard shows them ("Included in Pro"). */
function Meters({ p }: { p: ReturnType<typeof plan> }) {
  const rows = [
    { label: "Cursor Models", hint: "Includes Cursor Grok and Composer", v: p.auto, note: "Beyond the limit it uses the Other Models quota or on-demand spend." },
    { label: "Other Models", hint: "", v: p.api, note: "Beyond the limit it uses on-demand spend." },
  ].filter(r => r.v != null)
  if (!rows.length) return null
  return (
    <div className="space-y-3">
      {rows.map(r => (
        <div key={r.label}>
          <div className="flex items-baseline justify-between gap-3 text-sm">
            <span className="min-w-0">{r.label}{r.hint && <span className="text-muted-foreground"> · {r.hint}</span>}</span>
            <span className="num shrink-0">{Math.round(r.v!)}% used</span>
          </div>
          <Progress value={Math.min(100, r.v!)} className={cn("mt-1.5", r.v! > 85 && "[&_[data-slot=progress-indicator]]:bg-negative")} />
        </div>
      ))}
    </div>
  )
}

const daysLeft = (end: number, now: number) => Math.max(0, Math.ceil((end - now) / 86400))

/** How much Cursor the council has used, and how much of the plan is left. */
const OPEN_KEY = "solmeme.cursorUsage.open"

export function CursorUsageCard() {
  const { data, ok } = usePoll<CursorUsage>("api/cursor-usage", 60000, 25000)
  const [span, setSpan] = useState<"today" | "last_24h" | "cycle">("today")
  const [open, setOpenState] = useState(() => { try { return localStorage.getItem(OPEN_KEY) === "1" } catch { return false } })
  const setOpen = (v: boolean) => {
    setOpenState(v)
    try { localStorage.setItem(OPEN_KEY, v ? "1" : "0") } catch { /* private mode */ }
  }
  if (!data || !data.configured) return null
  const meter: CursorMeter | undefined = data[span]
  const byModel = new Map<string, string[]>()
  for (const j of data.judges ?? []) byModel.set(j.model, [...(byModel.get(j.model) ?? []), j.name.replace(/^cursor-/, "")])
  const acct = data.account
  const p = plan(acct?.summary)
  const now = data.now ?? acct?.fetched_at ?? 0           // the bridge's clock

  return (
    <Section
      title={<span className="flex items-center gap-2"><Gauge className="size-4" /> Cursor usage</span>}
      description={!ok ? "Can't reach the bot; showing the last numbers received." : open ? "What the Cursor judges used, and what is left on the plan." : undefined}
      action={<div className="flex shrink-0 items-center gap-1.5">
        <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
                className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-muted">
          {open ? "Hide details" : "Details"}<ChevronDown className={cn("size-3 transition-transform", open && "rotate-180")} />
        </button>
        <a href={USAGE_PAGE} target="_blank" rel="noreferrer"
           className="hidden items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-muted sm:inline-flex">
          Cursor's page <ExternalLink className="size-3" /></a>
      </div>}>
      {data.error && <p className="mb-3 text-xs text-negative">{data.error}</p>}

      {/* collapsed: just the balance and today's use */}
      {!open && (
        <div className="max-w-2xl space-y-3">
          {!acct?.configured ? <div className="text-sm text-muted-foreground">Plan balance not connected (open Details to set it up)</div>
            : !acct.ok ? <div className="text-sm text-negative">Plan balance unavailable</div>
            : p.unlimited ? <div className="text-sm">Plan{p.tier ? ` (${p.tier})` : ""}: <span className="font-medium">unlimited</span></div>
            : <Meters p={p} />}
          <div className="num flex flex-wrap justify-between gap-x-4 gap-y-0.5 text-xs text-muted-foreground">
            {data.today && (
              <span>council today: <span className="text-foreground">{data.today.calls}</span> calls · <span className="text-foreground">{tokens(data.today.input_tokens)}</span> in · <span className="text-foreground">{tokens(data.today.output_tokens)}</span> out</span>
            )}
            {p.end && <span>resets {dayClock(p.end)} ({daysLeft(p.end, now)} days left)</span>}
          </div>
        </div>
      )}

      {open && <>

      {/* the plan's real balance */}
      <div className="rounded-lg border p-3">
        {!acct?.configured ? (
          <div className="text-sm">
            <div className="font-medium">Plan balance: not connected</div>
            <ol className="mt-1 list-decimal space-y-0.5 pl-5 text-xs text-muted-foreground">
              <li>Sign in at cursor.com, open DevTools → Application → Cookies → https://cursor.com.</li>
              <li>Copy the value of <span className="font-mono">WorkosCursorSessionToken</span>.</li>
              <li>On the Cursor container: <span className="font-mono">pct enter 107</span>, then paste it after
                <span className="font-mono"> CURSOR_SESSION_COOKIE=</span> in <span className="font-mono">/etc/cursor-shim.env</span>,
                then <span className="font-mono">systemctl restart cursor-shim</span>.</li>
            </ol>
            <p className="mt-1.5 text-xs text-muted-foreground">That cookie is a full login to your Cursor account; it stays on that container. The numbers come from Cursor's own (undocumented) dashboard endpoint, so they can stop working if Cursor changes it.</p>
          </div>
        ) : !acct.ok ? (
          <div className="text-sm">
            <div className="font-medium text-negative">Plan balance unavailable</div>
            <p className="mt-0.5 text-xs text-muted-foreground">{acct.error}. Paste a fresh <span className="font-mono">WorkosCursorSessionToken</span> into
              <span className="font-mono"> /etc/cursor-shim.env</span> on container 107 and restart <span className="font-mono">cursor-shim</span>.</p>
          </div>
        ) : (
          <div className="space-y-2 text-sm">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <div>
                <span className="text-muted-foreground">Plan{p.tier ? ` (${p.tier})` : ""}: </span>
                {p.unlimited ? <span className="font-medium">unlimited</span> : (
                  <span className="num font-medium">{dollars(p.used)} of {dollars(p.limit)} used
                    {p.remaining != null && <span className="text-positive"> · {dollars(p.remaining)} left</span>}</span>
                )}
              </div>
              <span className="text-xs text-muted-foreground">
                {p.end ? `resets ${dayClock(p.end)}` : ""}{acct.fetched_at ? ` · checked ${ago(acct.fetched_at, now)}` : ""}
              </span>
            </div>
            {p.pctUsed != null && !p.unlimited && (
              <Progress value={Math.min(100, p.pctUsed)} className={cn(p.pctUsed > 85 && "[&_[data-slot=progress-indicator]]:bg-negative")} />
            )}
            <div className="pt-1"><Meters p={p} /></div>
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
              {p.pctUsed != null && <span>{p.pctUsed.toFixed(1)}% of the plan's dollars</span>}
              {p.total != null && <span>{p.total.toFixed(1)}% of all included usage</span>}
              {p.onDemandLimit != null && <span>on-demand {dollars(p.onDemandUsed)} of {dollars(p.onDemandLimit)}</span>}
            </div>
            <details className="text-xs text-muted-foreground">
              <summary className="cursor-pointer select-none">everything Cursor returned</summary>
              <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded bg-muted/40 p-2 font-mono [overflow-wrap:anywhere]">{JSON.stringify(acct.summary, null, 2)}</pre>
            </details>
          </div>
        )}
      </div>

      {/* the bridge's own meter */}
      <div className="mt-3">
        <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
          <span className="text-muted-foreground">Used by the council:</span>
          {(["today", "last_24h", "cycle"] as const).map(k => (
            <button key={k} type="button" onClick={() => setSpan(k)}
                    className={cn("rounded px-2 py-0.5", span === k ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted/60")}>
              {k === "today" ? "Today" : k === "last_24h" ? "24 h" : data.cycle_from === "billing cycle" ? "This billing cycle" : "This month"}
            </button>
          ))}
        </div>
        {!meter || meter.calls === 0 ? (
          <p className="text-xs text-muted-foreground">No Cursor calls in this period yet (the meter started Oct 9, 07:10).</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[34rem] text-xs">
              <thead className="text-muted-foreground">
                <tr className="text-left">
                  <th className="py-1 pr-2 font-normal">model</th><th className="py-1 pr-2 font-normal">judge</th>
                  <th className="py-1 pr-2 text-right font-normal">calls</th><th className="py-1 pr-2 text-right font-normal">tokens in</th>
                  <th className="py-1 pr-2 text-right font-normal">of which cached</th><th className="py-1 pr-2 text-right font-normal">tokens out</th>
                  <th className="py-1 text-right font-normal">avg time</th>
                </tr>
              </thead>
              <tbody className="num">
                {meter.models.map(r => (
                  <tr key={r.model} className="border-t">
                    <td className="py-1 pr-2 font-mono">{r.model}</td>
                    <td className="py-1 pr-2 text-muted-foreground">{(byModel.get(r.model) ?? ["—"]).join(", ")}</td>
                    <td className="py-1 pr-2 text-right">{r.ok}{r.ok !== r.calls && <span className="text-negative">/{r.calls}</span>}</td>
                    <td className="py-1 pr-2 text-right">{tokens(r.input_tokens)}</td>
                    <td className="py-1 pr-2 text-right text-muted-foreground">{tokens(r.cache_read)}</td>
                    <td className="py-1 pr-2 text-right">{tokens(r.output_tokens)}</td>
                    <td className="py-1 text-right">{r.avg_s != null ? `${Math.round(r.avg_s)}s` : "—"}</td>
                  </tr>
                ))}
                <tr className="border-t font-medium">
                  <td className="py-1 pr-2" colSpan={2}>total</td>
                  <td className="py-1 pr-2 text-right">{meter.ok}{meter.ok !== meter.calls && <span className="text-negative">/{meter.calls}</span>}</td>
                  <td className="py-1 pr-2 text-right">{tokens(meter.input_tokens)}</td>
                  <td className="py-1 pr-2 text-right text-muted-foreground">{tokens(meter.cache_read)}</td>
                  <td className="py-1 pr-2 text-right">{tokens(meter.output_tokens)}</td>
                  <td className="py-1" />
                </tr>
              </tbody>
            </table>
            <p className="mt-1.5 text-[11px] text-muted-foreground">Counts every question the council sent through the Cursor bridge, with the token counts Cursor reports. Each call carries a few thousand tokens of Cursor's own instructions on top of the council's question. Red = calls that failed.</p>
          </div>
        )}
      </div>
      </>}
    </Section>
  )
}
