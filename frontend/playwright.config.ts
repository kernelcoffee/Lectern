// E2E configuration — drives the REAL stack, not mocks: a dedicated backend
// (uvicorn on :8010, data under repo-root/.e2e-data) and a dedicated Vite dev
// server (:5174) proxying to it. Ports are offset so a normally-running dev
// or compose stack (:8000/:5173) is never touched.
//
// Two tiers, selected by the E2E_FULL env var:
//   * default        — fast smoke tests; only light catalog API calls
//                      (Mojang/Fabric version lists, disk-cached between runs).
//   * E2E_FULL=1     — also runs tests tagged @full: real server install and
//                      real Modrinth mod installs. First run downloads a JRE
//                      (~45 MB) into .e2e-data/java, cached afterwards.
//
// The database and server dirs are wiped per run — inside the backend's
// launch command, NOT in globalSetup: Playwright starts web servers before
// globalSetup runs, and deleting the SQLite file under an initialized engine
// leaves the next pooled connection staring at an empty, table-less database.
// The java/ + cache/ dirs persist to keep repeat runs fast.

import { defineConfig } from "@playwright/test";

const FULL = !!process.env.E2E_FULL;

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  grepInvert: FULL ? undefined : /@full/,
  // The suite mutates one shared backend — keep tests sequential.
  workers: 1,
  fullyParallel: false,
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:5174",
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command:
        "bash -c 'cd ../backend && rm -f ../.e2e-data/lectern.sqlite && rm -rf ../.e2e-data/servers && source .venv/bin/activate && LECTERN_DATA=../.e2e-data exec uvicorn lectern.main:app --port 8010'",
      url: "http://localhost:8010/api/health",
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: "npm run dev -- --port 5174 --strictPort",
      env: { VITE_PROXY_TARGET: "http://localhost:8010" },
      url: "http://localhost:5174",
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
});
