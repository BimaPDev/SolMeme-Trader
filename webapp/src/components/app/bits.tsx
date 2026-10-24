import { useMemo, useState, type ReactNode } from "react"
import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react"
import { Line, LineChart, ReferenceLine, YAxis } from "recharts"

import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { TableHead } from "@/components/ui/table"
import { cn } from "@/lib/utils"
import { splitKey } from "@/lib/format"

export function Stat({ label, value, sub, tone, children, className }: {
  label: string
  value: ReactNode
  sub?: ReactNode
  tone?: string
  children?: ReactNode
  className?: string
}) {
  return (
    <Card size="sm" className={className}>
      <CardHeader>
        <CardDescription className="text-xs font-medium uppercase tracking-wide">{label}</CardDescription>
        <CardTitle className={cn("num truncate text-xl font-semibold sm:text-2xl", tone)}>{value}</CardTitle>
      </CardHeader>
      {(sub || children) && (
        <CardContent className="min-w-0 space-y-2 text-xs text-muted-foreground [overflow-wrap:anywhere]">
          {sub && <div className="num">{sub}</div>}
          {children}
        </CardContent>
      )}
    </Card>
  )
}

export function Section({ title, description, action, children, className, contentClassName }: {
  title: ReactNode
  description?: ReactNode
  action?: ReactNode
  children: ReactNode
  className?: string
  contentClassName?: string
}) {
  return (
    <Card className={className}>
      <CardHeader className="flex flex-row items-start justify-between gap-4">
        <div className="min-w-0 space-y-1">
          <CardTitle>{title}</CardTitle>
          {description && <CardDescription className="[overflow-wrap:anywhere]">{description}</CardDescription>}
        </div>
        {action}
      </CardHeader>
      <CardContent className={cn("min-w-0", contentClassName)}>{children}</CardContent>
    </Card>
  )
}

const ROLE_STYLE: Record<string, string> = {
  champion: "bg-chart-3/15 text-chart-3",
  challenger: "bg-chart-2/15 text-chart-2",
  trial: "bg-chart-4/15 text-chart-4",
  swarm: "bg-chart-1/15 text-chart-1",
  newborn: "bg-chart-1/15 text-chart-1",
  dead: "bg-negative/15 text-negative",
  retired: "bg-muted text-muted-foreground",
  candidate: "bg-muted text-muted-foreground",
}

export function RoleBadge({ role }: { role: string }) {
  return <Badge className={cn("capitalize", ROLE_STYLE[role] ?? "bg-muted text-muted-foreground")}>{role}</Badge>
}

export function StrategyName({ k, className }: { k: string; className?: string }) {
  const [id, v] = splitKey(k)
  return (
    <span className={cn("inline-flex min-w-0 items-baseline gap-1.5", className)}>
      <span className="truncate font-medium">{id}</span>
      <span className="font-mono text-[11px] text-muted-foreground">{v}</span>
    </span>
  )
}

/** Tiny line, green when it ends above its baseline, red below. */
export function Sparkline({ points, baseline, width = 96, height = 28 }: {
  points: [number, number | null][]
  baseline: number
  width?: number
  height?: number
}) {
  const data = points.filter(p => p[1] != null).map(([t, v]) => ({ t, v }))
  if (data.length < 2) return <div style={{ width, height }} className="flex items-center"><div className="h-px w-full bg-border" /></div>
  const last = data[data.length - 1].v as number
  const color = last > baseline ? "var(--positive)" : last < baseline ? "var(--negative)" : "var(--muted-foreground)"
  return (
    <LineChart width={width} height={height} data={data} margin={{ top: 2, right: 2, bottom: 2, left: 2 }}>
      <YAxis hide domain={["dataMin", "dataMax"]} />
      <ReferenceLine y={baseline} stroke="var(--border)" strokeDasharray="2 2" />
      <Line type="monotone" dataKey="v" stroke={color} strokeWidth={1.5} dot={false} isAnimationActive={false} />
    </LineChart>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-sm text-muted-foreground">{children}</p>
}

export function Dot({ ok }: { ok: boolean }) {
  return (
    <span className="relative flex size-2">
      {ok && <span className="absolute inline-flex size-full animate-ping rounded-full bg-positive opacity-60" />}
      <span className={cn("relative inline-flex size-2 rounded-full", ok ? "bg-positive" : "bg-negative")} />
    </span>
  )
}


// ── sortable tables ─────────────────────────────────────────────────────────
type SortVal = number | string | null | undefined
export type Sort = { key: string; dir: "desc" | "asc" } | null

/** First signed number in a display string ("-0.008 SOL (-$0.92)" → -0.008, "0.94x now" → 0.94). */
export function lead(s: string | null | undefined): number | null {
  const m = (s ?? "").replace(/,/g, "").match(/[-+]?\d*\.?\d+(?:e[-+]?\d+)?/i)
  return m ? Number(m[0]) : null
}

/** "42s" / "7m" / "3h" / "2d 4h" → seconds. */
export function ageSeconds(s: string | null | undefined): number | null {
  let total = 0, any = false
  for (const [, n, u] of (s ?? "").matchAll(/(\d+(?:\.\d+)?)\s*([smhd])/g)) {
    total += Number(n) * ({ s: 1, m: 60, h: 3600, d: 86400 } as Record<string, number>)[u]
    any = true
  }
  return any ? total : null
}

function loadSort(id: string): Sort {
  try {
    const v = JSON.parse(localStorage.getItem(`solmeme.sort.${id}`) || "null")
    return v && typeof v.key === "string" && (v.dir === "asc" || v.dir === "desc") ? v : null
  } catch {
    return null
  }
}

/** Tap a column: highest first, again: lowest first, again: back to the default order. Empty values sort last.
 * The choice is remembered per table in this browser (survives a refresh). */
export function useSort<T>(rows: T[], get: Record<string, (r: T) => SortVal>, id: string) {
  const [sort, setSortState] = useState<Sort>(() => loadSort(id))
  const setSort = (next: Sort | ((cur: Sort) => Sort)) =>
    setSortState(cur => {
      const v = typeof next === "function" ? next(cur) : next
      try {
        if (v) localStorage.setItem(`solmeme.sort.${id}`, JSON.stringify(v))
        else localStorage.removeItem(`solmeme.sort.${id}`)
      } catch { /* private mode: sort just won't be remembered */ }
      return v
    })
  const sorted = useMemo(() => {
    if (!sort || !get[sort.key]) return rows
    const f = get[sort.key], sign = sort.dir === "desc" ? -1 : 1
    return [...rows].sort((a, b) => {
      const x = f(a), y = f(b)
      const xn = x == null || x === "" || (typeof x === "number" && Number.isNaN(x))
      const yn = y == null || y === "" || (typeof y === "number" && Number.isNaN(y))
      if (xn || yn) return xn === yn ? 0 : xn ? 1 : -1
      if (typeof x === "number" && typeof y === "number") return sign * (x - y)
      return sign * String(x).localeCompare(String(y))
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, sort])
  const toggle = (key: string) =>
    setSort(cur => (!cur || cur.key !== key ? { key, dir: "desc" } : cur.dir === "desc" ? { key, dir: "asc" } : null))
  return { sorted, sort, toggle }
}

export function SortHead({ k, sort, toggle, children, className, right }: {
  k: string; sort: Sort; toggle: (k: string) => void; children: ReactNode; className?: string; right?: boolean
}) {
  const on = sort?.key === k
  const Icon = !on ? ArrowUpDown : sort.dir === "desc" ? ArrowDown : ArrowUp
  return (
    <TableHead className={cn(right && "text-right", className)} aria-sort={on ? (sort.dir === "desc" ? "descending" : "ascending") : "none"}>
      <button type="button" onClick={() => toggle(k)}
        className={cn("inline-flex items-center gap-1 select-none hover:text-foreground", right && "flex-row-reverse", on && "text-foreground")}>
        {children}
        <Icon className={cn("size-3", on ? "opacity-100" : "opacity-35")} />
      </button>
    </TableHead>
  )
}

/** Sort buttons for card lists (phones), where there are no column headers. */
export function SortChips({ options, sort, toggle, className }: {
  options: [string, string][]; sort: Sort; toggle: (k: string) => void; className?: string
}) {
  return (
    <div className={cn("-mx-1 flex gap-1.5 overflow-x-auto px-1 pb-2 [scrollbar-width:none]", className)}>
      <span className="shrink-0 self-center text-xs text-muted-foreground">Sort</span>
      {options.map(([k, label]) => {
        const on = sort?.key === k
        const Icon = !on ? ArrowUpDown : sort.dir === "desc" ? ArrowDown : ArrowUp
        return (
          <button key={k} type="button" onClick={() => toggle(k)}
            className={cn("inline-flex shrink-0 items-center gap-1 rounded-full border px-2.5 py-1 text-xs", on ? "border-foreground/40 bg-muted text-foreground" : "text-muted-foreground")}>
            {label}<Icon className="size-3" />
          </button>
        )
      })}
    </div>
  )
}

/** The first `n` items, and a button for the rest: keeps long lists from making the page endless. */
export function ShowMore<T>({ items, n, children, className }: {
  items: T[]; n: number; children: (shown: T[]) => ReactNode; className?: string
}) {
  const [all, setAll] = useState(false)
  return (
    <>
      {children(all ? items : items.slice(0, n))}
      {items.length > n && (
        <button type="button" onClick={() => setAll(v => !v)}
          className={cn("mt-2 w-full rounded-md border py-1.5 text-xs text-muted-foreground hover:bg-muted hover:text-foreground", className)}>
          {all ? "show fewer" : `show all ${items.length}`}
        </button>
      )}
    </>
  )
}
