import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The runtime serves the built console from web/dist; in development vite
// proxies the API so the console reloads without a rebuild.
const api = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
  server: {
    port: 5175,
    proxy: {
      "/health": api,
      "/workflows": api,
      "/runs": api,
      "/effects": api,
      "/sweep": api,
      "/recover": api,
    },
  },
});
