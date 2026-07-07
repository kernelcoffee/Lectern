import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies API + WebSocket traffic to the FastAPI backend so the
// frontend can call same-origin "/api" and "/ws" paths in development.
// The backend target is configurable so the same config works locally
// (localhost) and inside docker-compose (http://backend:8000).
const target = process.env.VITE_PROXY_TARGET ?? "http://localhost:8000";
const wsTarget = target.replace(/^http/, "ws");

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target,
        changeOrigin: true,
      },
      "/ws": {
        target: wsTarget,
        ws: true,
      },
    },
  },
});
