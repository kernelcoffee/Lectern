// @full — Server Management Protocol roster: a Fabric server on the newest
// release (26.3 at the time of writing; MSMP since 1.21.9) gets the
// management server enabled by Lectern at start, the server page shows the
// connected client, and a Carpet bot spawned from the console appears in the
// Players tab's online list (flagged as a bot) and disappears when killed.
// Phase 2 on the same running server: a whitelist add/remove from the Players
// tab, a live MOTD change on the Properties tab ("applied now", the server
// rewrites server.properties itself) and a backup whose timeline event says
// the world was flushed through the protocol.
//
// Run with:  E2E_FULL=1 npx playwright test
// Network: Fabric server jar, Temurin JRE (cached in .e2e-data/java), Modrinth
// API + the Carpet jar, Mojang (registry lookup of "Notch", and the server's own
// name resolution for the whitelist add). Port: binds 25565 on the host; 02 deletes its server
// before this spec runs (sequential, alphabetical), so the port is free.

import { test, expect } from "@playwright/test";

const REGISTRY_NAME = "Notch";
// Set once this run registers Notch: the cleanup below removes him only then,
// so a registry that already had him is left alone.
let registeredHere = false;

// Leave the registry as it was found, whether or not the test passed.
test.afterEach(async ({ request }) => {
  if (!registeredHere) return;
  const players: { uuid: string; name: string }[] = await (
    await request.get("/api/players")
  ).json();
  const notch = players.find((p) => p.name === REGISTRY_NAME);
  if (notch) expect((await request.delete(`/api/players/${notch.uuid}`)).ok()).toBe(true);
});

test("MSMP roster: Carpet bot spawn and kill on the newest Fabric @full", async ({
  page,
}) => {
  test.setTimeout(600_000);

  // --- create a Fabric server on the preselected newest release -----------
  await page.goto("/");
  await page.locator("aside").getByRole("button", { name: "New server" }).click();
  await page.getByLabel("Server type").selectOption("fabric", { timeout: 15_000 });
  // The Minecraft version is NOT pinned: the form preselects the newest
  // release, which is the one that carries the management protocol.
  await expect(page.getByLabel("Fabric loader build")).not.toHaveValue("", {
    timeout: 15_000,
  });
  await page.getByLabel("Server name").fill("e2e-msmp");
  const build = page.getByRole("button", { name: "Build server" });
  await expect(build).toBeEnabled({ timeout: 15_000 });
  await build.click();

  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  const row = page.locator("tr", { hasText: "e2e-msmp" });
  await expect(row.getByText("stopped", { exact: true })).toBeVisible({
    timeout: 300_000,
  });

  // --- open, accept the EULA ----------------------------------------------
  await row.getByRole("button", { name: "e2e-msmp" }).click();
  await page.getByRole("button", { name: "Accept EULA" }).click();
  await expect(page.getByText(/accept the/i)).toBeHidden();

  // --- install Carpet from Modrinth ---------------------------------------
  await page.getByRole("button", { name: "Mods", exact: true }).click();
  await page.getByRole("button", { name: "Add mods" }).click();
  const browser = page.getByTestId("content-browser");
  await browser.getByPlaceholder("Search mods…").fill("carpet");
  const carpetRow = browser
    .locator("li")
    // `has` is resolved inside each <li>, so the inner locator must start from
    // the page, not from `browser` (that would look for the testid in the li).
    .filter({ has: page.getByText("Carpet", { exact: true }) })
    .first();
  await expect(carpetRow).toBeVisible({ timeout: 20_000 });
  await carpetRow.getByRole("button", { name: "Install" }).click();
  await expect(browser.getByText(/^Installed: Carpet/)).toBeVisible({
    timeout: 60_000,
  });
  await browser.getByRole("button", { name: "Close", exact: true }).click();

  // --- start; the management client connects ------------------------------
  const statusChip = page.getByTestId("status-chip");
  await page.getByRole("button", { name: "Start", exact: true }).click();
  await expect(statusChip).toHaveText("running", { timeout: 300_000 });
  await expect(page.getByTestId("msmp-badge")).toBeVisible({ timeout: 30_000 });

  // --- spawn a Carpet bot from the console --------------------------------
  await page.getByRole("button", { name: "Console", exact: true }).click();
  // Commands typed before the console socket opens are dropped — wait for it.
  await expect(page.getByText("console connected")).toBeVisible();
  const consoleInput = page.getByPlaceholder(/type a server command/);
  await consoleInput.fill("player Steve spawn");
  await consoleInput.press("Enter");

  // --- Players tab: Steve is online, flagged as a bot (5 s poll) ----------
  // Scoped to <main>: the sidebar also has a "Players" page button.
  await page.locator("main").getByRole("button", { name: "Players", exact: true }).click();
  // The subtitle names the active roster source.
  await expect(page.getByText("live via the management protocol")).toBeVisible();
  // Anchor on Steve's own card (keyed by its kill-bot control, which only a
  // bot gets) and check the bot tag inside that card, not anywhere on the page.
  const steve = page
    .getByTestId("online-player")
    .filter({ has: page.getByRole("button", { name: "Kill bot Steve" }) });
  await expect(steve).toHaveCount(1, { timeout: 20_000 });
  await expect(steve.getByText("Steve", { exact: true })).toBeVisible();
  await expect(steve.getByText("bot", { exact: true })).toBeVisible();

  // Proof the entry came from MSMP, not the console fallback: a Carpet bot's
  // console login line carries no UUID, while MSMP's players/joined does
  // (its offline UUID). Without this the spec passes with MSMP broken.
  const servers: { id: string; name: string }[] = await (
    await page.request.get("/api/servers")
  ).json();
  const id = servers.find((s) => s.name === "e2e-msmp")!.id;
  const online: { name: string; uuid: string | null; bot: boolean }[] = await (
    await page.request.get(`/api/servers/${id}/players/online`)
  ).json();
  expect(online).toEqual([
    expect.objectContaining({
      name: "Steve",
      bot: true,
      uuid: expect.stringMatching(/^[0-9a-f]{32}$/),
    }),
  ]);

  // The kill button is only visible on hover; force the click.
  await page.getByRole("button", { name: "Kill bot Steve" }).click({ force: true });
  await expect(page.getByRole("button", { name: "Kill bot Steve" })).toHaveCount(0, {
    timeout: 20_000,
  });
  await expect(page.getByText("Nobody online")).toBeVisible({ timeout: 20_000 });

  // --- player lists through the protocol ----------------------------------
  // Register Notch (real Mojang lookup), then whitelist him on the running
  // server: the add goes through allowlist/add and the server rewrites
  // whitelist.json, which is what the list (and the API) read back.
  type RegistryPlayer = { uuid: string; name: string };
  const registryBefore: RegistryPlayer[] = await (await page.request.get("/api/players")).json();
  await page.locator("aside").getByRole("button", { name: "Players", exact: true }).click();
  if (!registryBefore.some((p) => p.name === REGISTRY_NAME)) {
    registeredHere = true;
    await page.getByPlaceholder("Minecraft username or UUID").fill(REGISTRY_NAME);
    await page.getByRole("button", { name: "Add player" }).click();
  }
  await expect(page.getByText(REGISTRY_NAME, { exact: true })).toBeVisible({ timeout: 20_000 });
  const registry: RegistryPlayer[] = await (await page.request.get("/api/players")).json();
  const notch = registry.find((p) => p.name === REGISTRY_NAME)!.uuid;

  await page.locator("aside").getByRole("button", { name: /e2e-msmp/ }).click();
  await page.locator("main").getByRole("button", { name: "Players", exact: true }).click();
  const whitelist = page.getByRole("region", { name: "Whitelist" });
  await whitelist.getByLabel("Player to add to Whitelist").selectOption({ label: "Notch" });
  await whitelist.getByRole("button", { name: "Add", exact: true }).click();
  const notchRow = whitelist.getByRole("listitem").filter({ hasText: "Notch" });
  await expect(notchRow).toBeVisible({ timeout: 10_000 });
  type Lists = Record<"whitelist" | "ops" | "banned", { uuid: string; name: string }[]>;
  const listsAfterAdd: Lists = await (
    await page.request.get(`/api/servers/${id}/playerlists`)
  ).json();
  expect(listsAfterAdd.whitelist).toEqual([{ uuid: notch, name: "Notch" }]);

  await notchRow.getByRole("button", { name: "remove" }).click();
  await expect(notchRow).toHaveCount(0, { timeout: 10_000 });
  const listsAfterRemove: Lists = await (
    await page.request.get(`/api/servers/${id}/playerlists`)
  ).json();
  expect(listsAfterRemove.whitelist).toEqual([]);

  // Proof both changes went through the protocol, not the console fallback:
  // the console rung's `whitelist add|remove` prints vanilla command feedback
  // to the server log, the protocol rung prints none. A `say` marker typed
  // after the remove shows the log is read past both changes.
  await page.getByRole("button", { name: "Console", exact: true }).click();
  await expect(page.getByText("console connected")).toBeVisible();
  await consoleInput.fill("say e2e-lists-done");
  await consoleInput.press("Enter");
  const latestLog = async (): Promise<string> => {
    const file: { content: string | null } = await (
      await page.request.get(`/api/servers/${id}/files/content?path=logs%2Flatest.log`)
    ).json();
    return file.content ?? "";
  };
  await expect.poll(latestLog, { timeout: 20_000 }).toContain("e2e-lists-done");
  const log = await latestLog();
  expect(log).not.toContain("Added Notch to the whitelist");
  expect(log).not.toContain("Removed Notch from the whitelist");

  // --- live MOTD through serversettings/motd/set ---------------------------
  await page.getByRole("button", { name: "Properties", exact: true }).click();
  const motd = page.locator("label", { hasText: /^motd/ });
  await motd.locator("input").fill("e2e live motd");
  await expect(motd).toContainText("— applies now");
  await page
    .locator("section", { has: page.getByRole("heading", { name: "server.properties" }) })
    .getByRole("button", { name: "Save", exact: true })
    .click();
  await expect(motd).toContainText("— applied now", { timeout: 10_000 });
  // Lectern did not write the key: the server rewrote its own file.
  const props: { properties: Record<string, string> } = await (
    await page.request.get(`/api/servers/${id}/properties`)
  ).json();
  expect(props.properties.motd).toBe("e2e live motd");

  // --- backup while running: flushed through server/save -------------------
  await page.getByRole("button", { name: "Backups", exact: true }).click();
  await page.getByRole("button", { name: "Create backup" }).click();
  await expect(page.getByText(/^Backup created \(/)).toBeVisible({ timeout: 120_000 });
  await page.getByRole("button", { name: "Metrics", exact: true }).click();
  // The events panel polls every 10 s.
  await expect(
    page.locator("li", { hasText: "Backup created" }).filter({
      hasText: /flushed via management protocol/,
    }),
  ).toHaveCount(1, { timeout: 20_000 });

  // --- stop from the console, delete --------------------------------------
  await page.getByRole("button", { name: "Console", exact: true }).click();
  await expect(page.getByText("console connected")).toBeVisible();
  await consoleInput.fill("stop");
  await consoleInput.press("Enter");
  await expect(statusChip).toHaveText("stopped", { timeout: 120_000 });

  // --- Properties: the keys Lectern wrote at start are shown read-only ------
  await page.getByRole("button", { name: "Properties", exact: true }).click();
  for (const key of [
    "management-server-enabled",
    "management-server-host",
    "management-server-port",
    "management-server-tls-enabled",
    "management-server-secret",
  ]) {
    const field = page
      .locator("label", { hasText: key })
      .filter({ hasText: "reserved for Lectern (management protocol)" });
    await expect(field).toHaveCount(1);
    await expect(field.locator("input")).toBeDisabled();
  }
  await expect(
    page.locator("label", { hasText: "management-server-host" }).locator("input"),
  ).toHaveValue("127.0.0.1");
  await expect(
    page.locator("label", { hasText: "management-server-tls-enabled" }).locator("input"),
  ).toHaveValue("false");

  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Delete server" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
});
