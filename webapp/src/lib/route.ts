import { useEffect, useState } from "react"

export const PAGES = [
  "overview", "positions", "trades", "strategies",
  "swarm", "council", "learning",
  "scanner", "narratives", "wallets",
  "sources",
  "live", "live-positions", "live-trades", "live-strategy", "live-council", "live-whales", "live-scanner", "live-narratives", "ask", "live-ask", "live-team", "live-research", "live-setups", "live-trader",
] as const
export type Page = (typeof PAGES)[number]

const OLD_TAB: Record<string, Page> = { market: "positions" }      // tabs remembered from the old layout

function read(): Page {
  const h = window.location.hash.replace(/^#\/?/, "").split(/[?/]/)[0]
  if ((PAGES as readonly string[]).includes(h)) return h as Page
  if (!h) {
    try {
      const old = localStorage.getItem("solmeme.tab") ?? ""
      if ((PAGES as readonly string[]).includes(old)) return old as Page
      if (OLD_TAB[old]) return OLD_TAB[old]
    } catch { /* private mode */ }
  }
  return "overview"
}

/** The page lives in the URL hash (#/swarm), so links, bookmarks and the back button work. */
export function useRoute() {
  const [page, setPage] = useState<Page>(read)
  useEffect(() => {
    const on = () => { setPage(read()); window.scrollTo({ top: 0 }) }
    window.addEventListener("hashchange", on)
    return () => window.removeEventListener("hashchange", on)
  }, [])
  useEffect(() => {
    try {
      localStorage.setItem("solmeme.tab", page)
      if (!page.startsWith("live")) localStorage.setItem("solmeme.paper_page", page)
    } catch { /* private mode */ }
  }, [page])
  return page
}

export const go = (p: Page) => { window.location.hash = `/${p}` }

/** Paper (learning and testing) vs Live (the real-size wallet). The page decides the mode. */
export type Mode = "paper" | "live"
export const modeOf = (p: Page): Mode => (p.startsWith("live") ? "live" : "paper")

/** Back to Paper: the paper page you were last on. */
export function lastPaperPage(): Page {
  try {
    const p = localStorage.getItem("solmeme.paper_page") ?? ""
    if ((PAGES as readonly string[]).includes(p) && !p.startsWith("live")) return p as Page
  } catch { /* private mode */ }
  return "overview"
}
export const href = (p: Page) => `#/${p}`
