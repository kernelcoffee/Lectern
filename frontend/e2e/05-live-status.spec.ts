// Smoke tier — the live status feed (WS /ws/status). The server list and the
// feed are both mocked: the list says "stopped", then the feed pushes a
// "running" transition and the dashboard chip, its Stop button and the
// sidebar dot must follow without any navigation or reload. The backend side
// (set_status → StatusHub → /ws/status) is covered by
// backend/tests/test_status_feed.py; the @full journey (02) drives it for real.

import { test, expect, Route } from "@playwright/test";

const ID = "e2e-live-status";
const server = {
  id: ID,
  name: "Live status stub",
  type: "vanilla",
  mc_version: "1.21",
  loader_version: null,
  port: 25598,
  memory_mb: 1024,
  status: "stopped",
  created_at: "2026-01-01T00:00:00",
};
const detail = {
  ...server,
  jvm_args: "",
  auto_start: false,
  auto_start_delay: 0,
  crash_restart: false,
  stop_command: "stop",
  shutdown_timeout: 30,
  log_retention_days: 7,
  backup_excluded: "",
  backup_max: 5,
  backup_compress: true,
  backup_stop_server: false,
  eula_accepted: true,
  running: false,
};

test("status feed: dashboard and sidebar follow a pushed transition", async ({ page }) => {
  const json = (route: Route, body: unknown) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

  await page.route(
    (url) => url.pathname.startsWith("/api/servers"),
    async (route) => {
      const { pathname } = new URL(route.request().url());
      const method = route.request().method();
      if (pathname === "/api/servers" && method === "GET") return json(route, [server]);
      if (pathname === `/api/servers/${ID}` && method === "GET") return json(route, detail);
      return route.fallback();
    },
  );

  await page.routeWebSocket(/\/ws\/status$/, (ws) => {
    ws.send(
      JSON.stringify({ type: "snapshot", servers: [{ server_id: ID, status: "stopped" }] }),
    );
    setTimeout(
      () => ws.send(JSON.stringify({ type: "status", server_id: ID, status: "running" })),
      300,
    );
  });

  await page.goto("/");
  const row = page.locator("tr", { hasText: "Live status stub" });
  const stop = row.getByRole("button", { name: "Stop" });
  const navItem = page.locator("aside").getByRole("button", { name: /Live status stub/ });

  // Initial state comes from the list (and the snapshot agrees)…
  await expect(row).toBeVisible();

  // …then the pushed transition lands everywhere without navigating.
  await expect(row.getByText("running", { exact: true })).toBeVisible();
  await expect(stop).toBeEnabled();
  await expect(navItem).toHaveAccessibleName(/running/);
});

test("status feed: a dropped feed stops overriding the REST list", async ({ page }) => {
  // REST always says "stopped". The first feed connection claims "running",
  // then drops; the reconnect sends nothing. The reconnect's list reload must
  // show the REST status: the hook empties its map on close, so a dead feed's
  // last values cannot be overlaid onto fresher REST data.
  const json = (route: Route, body: unknown) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

  await page.route(
    (url) => url.pathname.startsWith("/api/servers"),
    async (route) => {
      const { pathname } = new URL(route.request().url());
      const method = route.request().method();
      if (pathname === "/api/servers" && method === "GET") return json(route, [server]);
      if (pathname === `/api/servers/${ID}` && method === "GET") return json(route, detail);
      return route.fallback();
    },
  );

  // Dev StrictMode mounts the hook twice, so the page may close a connection
  // itself right away; only a connection still open when the timers fire
  // counts. The first such one claims "running" and is then dropped; every
  // later connection stays silent (no snapshot) so only REST sets the status.
  let dropped = false;
  let reconnected = false;
  await page.routeWebSocket(/\/ws\/status$/, (ws) => {
    if (dropped) {
      reconnected = true;
      return;
    }
    let open = true;
    ws.onClose(() => {
      open = false;
    });
    setTimeout(() => {
      if (!open || dropped) return;
      ws.send(
        JSON.stringify({ type: "snapshot", servers: [{ server_id: ID, status: "running" }] }),
      );
      setTimeout(() => {
        if (!open) return;
        dropped = true;
        ws.close();
      }, 1500);
    }, 300);
  });

  await page.goto("/");
  const row = page.locator("tr", { hasText: "Live status stub" });

  await expect(row.getByText("running", { exact: true })).toBeVisible();
  // Close → backoff (1 s) → reconnect → reload from REST.
  await expect.poll(() => reconnected, { timeout: 10_000 }).toBe(true);
  await expect(row.getByText("stopped", { exact: true })).toBeVisible();
});
