// Smoke tier — the management-protocol (MSMP) UI with a stubbed server: the
// server-page chip (connected / connecting / absent), the Players tab's
// roster-source line and bot kill, and the Properties tab's read-only managed
// keys. The backend side is covered by backend/tests/test_msmp.py; the @full
// spec 06 drives a real 26.3 Fabric server.

import { test, expect, Route, Page } from "@playwright/test";

const ID = "e2e-msmp-stub";
const server = {
  id: ID,
  name: "MSMP stub",
  type: "fabric",
  mc_version: "26.3",
  loader_version: "0.17.0",
  port: 25597,
  memory_mb: 1024,
  status: "running",
  created_at: "2026-01-01T00:00:00",
};
const base = {
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
  running: true,
};

const def = (managed: boolean, type = "string") => ({
  type,
  values: null,
  min: null,
  max: null,
  description: "",
  default: null,
  managed,
});

async function stub(
  page: Page,
  detail: object,
  opts: { online?: object[]; kicks?: string[] } = {},
) {
  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  let online = opts.online ?? [];
  await page.route(
    (url) => url.pathname.startsWith("/api/servers"),
    async (route) => {
      const { pathname } = new URL(route.request().url());
      const method = route.request().method();
      if (pathname === "/api/servers" && method === "GET") return json(route, [server]);
      if (pathname === `/api/servers/${ID}` && method === "GET") return json(route, detail);
      if (pathname === `/api/servers/${ID}/players/online` && method === "GET")
        return json(route, online);
      if (pathname === `/api/servers/${ID}/playerlists` && method === "GET")
        return json(route, { whitelist: [], ops: [], banned: [] });
      const kick = pathname.match(/\/players\/online\/([^/]+)\/kick$/);
      if (kick && method === "POST") {
        opts.kicks?.push(kick[1]);
        online = online.filter((p) => (p as { name: string }).name !== kick[1]);
        return json(route, online);
      }
      if (pathname === `/api/servers/${ID}/properties` && method === "GET")
        return json(route, {
          properties: {
            motd: "hi",
            "management-server-enabled": "true",
            "management-server-port": "25585",
            "management-server-secret": "a".repeat(40),
          },
          definitions: {
            motd: def(false),
            "management-server-enabled": def(true, "boolean"),
            "management-server-host": def(true),
            "management-server-port": def(true, "integer"),
            "management-server-tls-enabled": def(true, "boolean"),
            "management-server-secret": def(true),
          },
        });
      return route.fallback();
    },
  );
  await page.goto("/");
  await page.locator("aside").getByRole("button", { name: /MSMP stub/ }).click();
}

test("msmp: connected chip, roster source and bot kill", async ({ page }) => {
  const kicks: string[] = [];
  await stub(
    page,
    { ...base, msmp: { connected: true, schema_version: "3.1.0" } },
    {
      kicks,
      online: [
        {
          name: "Steve",
          uuid: "5b6b3b8e000030008000000000000001",
          bot: true,
          joined_at: 1_767_225_600,
        },
      ],
    },
  );
  const badge = page.getByTestId("msmp-badge");
  await expect(badge).toBeVisible();
  await expect(badge).toHaveAttribute("title", /schema 3\.1\.0/);
  await expect(page.getByTestId("msmp-pending")).toHaveCount(0);

  await page.locator("main").getByRole("button", { name: "Players", exact: true }).click();
  await expect(page.getByText("live via the management protocol")).toBeVisible();
  const steve = page
    .getByTestId("online-player")
    .filter({ has: page.getByRole("button", { name: "Kill bot Steve" }) });
  await expect(steve).toHaveCount(1);
  await expect(steve.getByText("bot", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Kill bot Steve" }).click({ force: true });
  await expect(page.getByText(/Nobody online/)).toBeVisible();
  expect(kicks).toEqual(["Steve"]);
});

test("msmp: a connecting client shows the pending chip and console source", async ({
  page,
}) => {
  await stub(page, { ...base, msmp: { connected: false, schema_version: null } });
  await expect(page.getByTestId("msmp-pending")).toBeVisible();
  await expect(page.getByTestId("msmp-badge")).toHaveCount(0);
  await page.locator("main").getByRole("button", { name: "Players", exact: true }).click();
  await expect(page.getByText("live from the server console")).toBeVisible();
});

test("msmp: no client (field absent) renders no chip", async ({ page }) => {
  // Older backends / stubs omit `msmp` entirely: the page must still render.
  await stub(page, base);
  await expect(page.getByRole("heading", { name: "MSMP stub" })).toBeVisible();
  await expect(page.getByTestId("msmp-badge")).toHaveCount(0);
  await expect(page.getByTestId("msmp-pending")).toHaveCount(0);
});

test("msmp: managed properties are read-only, unwritten ones hidden", async ({ page }) => {
  await stub(page, { ...base, msmp: { connected: true, schema_version: "3.1.0" } });
  await page.getByRole("button", { name: "Properties", exact: true }).click();
  for (const key of [
    "management-server-enabled",
    "management-server-port",
    "management-server-secret",
  ]) {
    const field = page
      .locator("label", { hasText: key })
      .filter({ hasText: "reserved for Lectern (management protocol)" });
    await expect(field).toHaveCount(1);
    await expect(field.locator("input")).toBeDisabled();
  }
  // Defined as managed but absent from the file: no empty read-only row.
  await expect(page.locator("label", { hasText: "management-server-host" })).toHaveCount(0);
  await expect(
    page.locator("label", { hasText: "management-server-tls-enabled" }),
  ).toHaveCount(0);
  // An ordinary key stays editable.
  await expect(page.locator("label", { hasText: /^motd/ }).locator("input")).toBeEnabled();
});
