import { StrictMode } from "react"
import { createRoot } from "react-dom/client"

import App from "@/App"
import { TooltipProvider } from "@/components/ui/tooltip"
import { InspectProvider } from "@/lib/inspect"
import "@/index.css"

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <TooltipProvider>
      <InspectProvider>
        <App />
      </InspectProvider>
    </TooltipProvider>
  </StrictMode>,
)
