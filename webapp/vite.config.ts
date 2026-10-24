import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// `npm run build` writes the site into ../web, which the bot's Python server serves.
// `npm run dev` proxies /api to a running bot (SOLMEME_API, default the homelab).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "./src") } },
  base: "./",
  build: { outDir: "../web", emptyOutDir: true, chunkSizeWarningLimit: 1200 },
  server: { proxy: { "/api": process.env.SOLMEME_API ?? "http://192.168.50.211" } },
})
