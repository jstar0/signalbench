import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Static replay build for GitHub Pages: PAGES_BASE selects the sub-path and
// switches the output to web/dist; without it the build is unchanged.
const pagesBase = process.env.PAGES_BASE;

export default defineConfig({
  base: pagesBase ?? "/",
  plugins: [react()],
  define: { "import.meta.env.VITE_REPLAY": JSON.stringify(process.env.VITE_REPLAY ?? "") },
  server: { proxy: { "/api": "http://127.0.0.1:5201" } },
  build: { outDir: pagesBase ? "dist" : "../src/signalbench/static", emptyOutDir: true },
});
