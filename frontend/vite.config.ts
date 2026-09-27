import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  base: "/ui/",
  build: { outDir: "../src/qq_ai_bot/webui/assets", emptyOutDir: true },
  server: {
    host: "127.0.0.1",
    port: 18765,
    strictPort: true,
    proxy: { "/api/control": "http://127.0.0.1:8080" },
  },
});
