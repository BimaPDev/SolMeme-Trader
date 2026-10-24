export const usd = (x: number | null | undefined, digits = 2) =>
  x == null || !Number.isFinite(x) ? "—" : `${x < 0 ? "-" : ""}$${Math.abs(x).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })}`

export const signedUsd = (x: number | null | undefined) => (x == null ? "—" : `${x >= 0 ? "+" : ""}${usd(x)}`)

export const sol = (x: number | null | undefined, digits = 3) =>
  x == null || !Number.isFinite(x) ? "—" : `${x >= 0 ? "" : "-"}${Math.abs(x).toFixed(digits)}`

export const signedSol = (x: number | null | undefined, digits = 3) =>
  x == null || !Number.isFinite(x) ? "—" : `${x >= 0 ? "+" : "-"}${Math.abs(x).toFixed(digits)}`

export const pct = (x: number | null | undefined, digits = 1, signed = false) =>
  x == null || !Number.isFinite(x) ? "—" : `${signed && x >= 0 ? "+" : ""}${x.toFixed(digits)}%`

export const num = (x: number | null | undefined, digits = 1, signed = false) =>
  x == null || !Number.isFinite(x) ? "—" : `${signed && x >= 0 ? "+" : ""}${x.toFixed(digits)}`

export const int = (x: number | null | undefined) => (x == null ? "—" : Math.round(x).toLocaleString())

export function duration(s: number | null | undefined) {
  if (s == null || !Number.isFinite(s)) return "—"
  s = Math.max(0, s)
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d) return `${d}d ${h}h`
  if (h) return `${h}h ${m}m`
  if (m) return `${m}m ${Math.floor(s % 60)}s`
  return `${Math.floor(s)}s`
}

export const ago = (ts: number | null | undefined, now = Date.now() / 1000) => (ts == null ? "—" : `${duration(now - ts)} ago`)

export const clock = (ts: number) =>
  new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })

/** Axis ticks: seconds when the chart spans under 10 minutes, so ticks don't repeat. */
export const clockFor = (spanS: number) => (ts: number) =>
  new Date(ts * 1000).toLocaleTimeString([], spanS < 600
    ? { hour: "2-digit", minute: "2-digit", second: "2-digit" }
    : { hour: "2-digit", minute: "2-digit" })

/** Fitness score; -100 is the "made no trades" sentinel from the replay scorer. */
export const fit = (x: number | null | undefined) =>
  x == null ? "—" : x <= -100 ? "no trades" : num(x, 1, true)

export const dayClock = (ts: number) =>
  new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })

export const tone = (x: number | null | undefined) =>
  x == null || x === 0 ? "text-muted-foreground" : x > 0 ? "text-positive" : "text-negative"

/** "dex_curve_momentum@6c55" → ["dex_curve_momentum", "6c55058"] */
export const splitKey = (key: string) => {
  const [id, v] = key.split("@")
  return [id, (v ?? "").slice(0, 7)] as const
}

/** Mutation signature → readable label. */
export function opLabel(sig: string) {
  const [kind, a, b] = sig.split(":")
  const dir = (d?: string) => (d === "+" ? "↑" : d === "-" ? "↓" : d ?? "")
  switch (kind) {
    case "param": return `${a} ${dir(b)}`
    case "rule_value": return `${a} ${dir(b)}`
    case "add_rule": return `+ ${a} ${b}`
    case "remove_rule": return `− ${a}`
    case "exit": return `${a.replace(/_pct|_s$|_x$/g, "")} ${dir(b)}`
    case "take_profit": return `TP ${dir(a)}`
    case "lesson": return `lesson: ${a} ${b}`
    case "sizing_pct": return `bet size ${dir(a)}`
    case "idea": return `council idea`
    case "winner_rule": return `winner's rule: ${a} ${b}`
    case "adopt_exits": return `exits from ${a}`
    case "crossover": return "crossover"
    default: return sig
  }
}
