import { createContext, useContext, useState, type ReactNode } from "react"

import { ProfileSheet } from "@/components/app/best"
import { CoinSheet } from "@/components/app/narratives"

type Inspect = { agent: (key: string) => void; coin: (mint: string) => void }

const Ctx = createContext<Inspect>({ agent: () => {}, coin: () => {} })

/** One place that opens a strategy's profile or a coin's forensics card, from any page or the search box. */
export function InspectProvider({ children }: { children: ReactNode }) {
  const [agent, setAgent] = useState<string | null>(null)
  const [coin, setCoin] = useState<string | null>(null)
  return (
    <Ctx.Provider value={{ agent: setAgent, coin: setCoin }}>
      {children}
      <ProfileSheet agentKey={agent} onClose={() => setAgent(null)} onOpen={setAgent} />
      {coin && <CoinSheet mint={coin} onClose={() => setCoin(null)} />}
    </Ctx.Provider>
  )
}

export const useInspect = () => useContext(Ctx)
