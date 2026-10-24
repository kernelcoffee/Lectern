// Smoke tier — the management-protocol (MSMP) UI with a stubbed server: the
// server-page chip (connected / connecting / absent), the Players tab's
// roster-source line, bot kill and ban reason, and the Properties tab's
// read-only managed keys and "applies now / at next start" hints. The backend
// side is covered by backend/tests/test_msmp.py; the @full spec 06 drives a
// real 26.3 Fabric server.

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

const def = (managed: boolean, type = "string", live = false) => ({
  type,
  values: null,
  min: null,
  max: null,
  description: "",
  default: null,
  managed,
  live,
});

const definitions = {
  motd: def(false, "string", true),
  difficulty: def(false, "string", true),
  pvp: def(false, "boolean"),
  "management-server-enabled": def(true, "boolean"),
  "management-server-host": def(true),
  "management-server-port": def(true, "integer"),
  "management-server-tls-enabled": def(true, "boolean"),
  "management-server-secret": def(true),
};
const fileProperties = {
  motd: "hi",
  pvp: "true",
  "management-server-enabled": "true",
  "management-server-port": "25585",
  "management-server-secret": "a".repeat(40),
};

const NOTCH = { uuid: "069a79f444e94726a5befca90e38aaf5", name: "Notch" };
type Lists = Record<"whitelist" | "ops" | "banned", { uuid: string; name: string }[]>;

async function stub(
  page: Page,
  detail: object,
  opts: {
    online?: object[];
    kicks?: string[];
    /** Captures PATCH …/properties bodies; the reply is `patchReply`. */
    patches?: unknown[];
    patchReply?: object;
    /** Captures POST …/playerlists/{kind} as {kind, body}. */
    listPosts?: { kind: string; body: unknown }[];
    /** Phone viewport: the sidebar is a drawer behind the hamburger. */
    phone?: boolean;
  } = {},
) {
  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  let online = opts.online ?? [];
  const lists: Lists = { whitelist: [], ops: [], banned: [] };
  await page.route(
    (url) => url.pathname === "/api/players",
    (route) =>
      route.request().method() === "GET"
        ? json(route, [{ ...NOTCH, added_at: "2026-01-01T00:00:00" }])
        : route.fallback(),
  );
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
        return json(route, lists);
      const listAdd = pathname.match(/\/playerlists\/(whitelist|ops|banned)$/);
      if (listAdd && method === "POST") {
        const kind = listAdd[1] as keyof Lists;
        opts.listPosts?.push({ kind, body: route.request().postDataJSON() });
        lists[kind] = [...lists[kind], NOTCH];
        return json(route, lists);
      }
      const kick = pathname.match(/\/players\/online\/([^/]+)\/kick$/);
      if (kick && method === "POST") {
        opts.kicks?.push(kick[1]);
        online = online.filter((p) => (p as { name: string }).name !== kick[1]);
        return json(route, online);
      }
      if (pathname === `/api/servers/${ID}/properties` && method === "GET")
        return json(route, { properties: fileProperties, definitions, applied_live: [] });
      if (pathname === `/api/servers/${ID}/properties` && method === "PATCH" && opts.patchReply) {
        opts.patches?.push(route.request().postDataJSON());
        return json(route, opts.patchReply);
      }
      return route.fallback();
    },
  );
  await page.goto("/");
  if (opts.phone) await page.getByRole("button", { name: "Open navigation" }).click();
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

const connected = { ...base, msmp: { connected: true, schema_version: "3.1.0" } };

/** The server.properties row whose label starts with `key`. */
const row = (page: Page, key: string) =>
  page.locator("label", { hasText: new RegExp(`^${key}`) });

test("msmp: live properties show applies-now hints", async ({ page }) => {
  const patches: unknown[] = [];
  await stub(page, connected, {
    patches,
    patchReply: {
      properties: { ...fileProperties, motd: "fresh motd", pvp: "false" },
      definitions,
      applied_live: ["motd"],
    },
  });
  await page.getByRole("button", { name: "Properties", exact: true }).click();
  await expect(page.getByText(/management protocol connected, motd/)).toBeVisible();

  // Untouched rows carry no hint.
  await expect(row(page, "motd")).not.toContainText("applies");

  // Editing: a live key applies now, a non-live one at the next start.
  await row(page, "motd").locator("input").fill("fresh motd");
  await expect(row(page, "motd")).toContainText("— applies now");
  await row(page, "pvp").locator("select").selectOption("false");
  await expect(row(page, "pvp")).toContainText("— applies at next start");

  const props = page.locator("section", {
    has: page.getByRole("heading", { name: "server.properties" }),
  });
  await props.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("1 setting applied now")).toBeVisible();
  expect(patches).toEqual([{ motd: "fresh motd", pvp: "false" }]);

  // After the save: what the response reported, per row.
  await expect(row(page, "motd")).toContainText("— applied now");
  await expect(row(page, "motd").locator("input")).toHaveValue("fresh motd");
  await expect(row(page, "pvp")).toContainText("— applies at next start");

  // Editing again replaces the post-save hint with the editing one.
  await row(page, "motd").locator("input").fill("fresher motd");
  await expect(row(page, "motd")).toContainText("— applies now");
  await expect(row(page, "motd")).not.toContainText("applied now");
});

test("msmp: disconnected client hints next start", async ({ page }) => {
  await stub(page, { ...base, msmp: { connected: false, schema_version: null } });
  await page.getByRole("button", { name: "Properties", exact: true }).click();
  await expect(page.getByText("Changes apply the next time the server starts.")).toBeVisible();
  await row(page, "motd").locator("input").fill("fresh motd");
  await expect(row(page, "motd")).toContainText("— applies at next start");
  await expect(row(page, "motd")).not.toContainText("applies now");
});

test("players: ban reason is sent", async ({ page }) => {
  const listPosts: { kind: string; body: unknown }[] = [];
  await stub(page, connected, { listPosts });
  await page.locator("main").getByRole("button", { name: "Players", exact: true }).click();

  const banned = page.getByRole("region", { name: "Banned" });
  await banned.getByLabel("Player to add to Banned").selectOption({ label: "Notch" });
  await banned.getByLabel("Ban reason").fill("griefing");
  await banned.getByRole("button", { name: "Add", exact: true }).click();
  await expect(banned.getByRole("listitem").filter({ hasText: "Notch" })).toBeVisible();
  // Inputs reset after a successful add.
  await expect(banned.getByLabel("Ban reason")).toHaveValue("");

  // The whitelist has no reason field and never sends one.
  const whitelist = page.getByRole("region", { name: "Whitelist" });
  await expect(whitelist.getByLabel("Ban reason")).toHaveCount(0);
  await whitelist.getByLabel("Player to add to Whitelist").selectOption({ label: "Notch" });
  await whitelist.getByRole("button", { name: "Add", exact: true }).click();
  await expect(whitelist.getByRole("listitem").filter({ hasText: "Notch" })).toBeVisible();

  expect(listPosts).toEqual([
    { kind: "banned", body: { uuid: NOTCH.uuid, reason: "griefing" } },
    { kind: "whitelist", body: { uuid: NOTCH.uuid } },
  ]);
});

test("players: a blank ban reason is left out", async ({ page }) => {
  const listPosts: { kind: string; body: unknown }[] = [];
  await stub(page, connected, { listPosts });
  await page.locator("main").getByRole("button", { name: "Players", exact: true }).click();
  const banned = page.getByRole("region", { name: "Banned" });
  await banned.getByLabel("Player to add to Banned").selectOption({ label: "Notch" });
  await banned.getByLabel("Ban reason").fill("   ");
  await banned.getByRole("button", { name: "Add", exact: true }).click();
  await expect(banned.getByRole("listitem").filter({ hasText: "Notch" })).toBeVisible();
  expect(listPosts).toEqual([{ kind: "banned", body: { uuid: NOTCH.uuid } }]);
});

test.describe("phone", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("msmp: Players and Properties tabs fit a phone", async ({ page }) => {
    await stub(page, connected, { phone: true });
    const noSideScroll = async (label: string) => {
      const { scrollWidth, clientWidth } = await page.evaluate(() => ({
        scrollWidth: document.scrollingElement!.scrollWidth,
        clientWidth: document.scrollingElement!.clientWidth,
      }));
      expect(scrollWidth, `${label} scrolls horizontally`).toBeLessThanOrEqual(clientWidth);
    };
    await page.locator("main").getByRole("button", { name: "Players", exact: true }).click();
    await expect(page.getByLabel("Ban reason")).toBeVisible();
    await noSideScroll("players tab");
    await page.getByRole("button", { name: "Properties", exact: true }).click();
    await row(page, "motd").locator("input").fill("fresh motd");
    await expect(row(page, "motd")).toContainText("— applies now");
    await noSideScroll("properties tab");
  });
});
