// @full — the real end-to-end journey, from the docs/functional.md §5.1 happy
// path (minus starting Minecraft, which backend tests cover): create a Fabric
// server on the New-server page, watch the dashboard follow the install,
// manage mods against real Modrinth, edit a property, delete the server.
//
// Run with:  E2E_FULL=1 npx playwright test
// Network: Fabric server jar (~1 MB), Temurin JRE (~45 MB, cached in
// .e2e-data/java after the first run), Modrinth API + one mod jar.

import { test, expect } from "@playwright/test";

test("full server journey: create → mods → properties → delete @full", async ({
  page,
}) => {
  test.setTimeout(600_000);

  // --- create a Fabric 1.20.1 server on the New-server page ----------------
  await page.goto("/");
  await page.locator("aside").getByRole("button", { name: "New server" }).click();
  await page.getByLabel("Server type").selectOption("fabric", {
    timeout: 15_000,
  });
  // Wait for the fabric reload (loader field appears with the newest build
  // for the auto-preselected latest version), then pin MC 1.20.1.
  await expect(page.getByLabel("Fabric loader build")).not.toHaveValue("", {
    timeout: 15_000,
  });
  await page.getByLabel("Minecraft version").selectOption("1.20.1", {
    timeout: 15_000,
  });
  await page.getByLabel("Server name").fill("e2e-journey");
  // Build waits out the loader refetch for 1.20.1 (disabled while loading).
  const build = page.getByRole("button", { name: "Build server" });
  await expect(build).toBeEnabled({ timeout: 15_000 });
  await build.click();

  // Back on the dashboard: the table row appears in the sidebar + table and
  // flips from installing to stopped once jar + JRE are in place.
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  const row = page.locator("tr", { hasText: "e2e-journey" });
  await expect(row).toBeVisible();
  await expect(row.getByText("stopped", { exact: true })).toBeVisible({
    timeout: 300_000,
  });

  // --- conflict guard: same name (or port) is refused with a clear 409 ----
  await page.locator("aside").getByRole("button", { name: "New server" }).click();
  // The suggestion iterates past the taken port (e2e-journey holds 25565).
  await expect(page.getByLabel("Server port")).toHaveValue("25566", {
    timeout: 10_000,
  });
  await page.getByLabel("Minecraft version").selectOption("1.20.1", {
    timeout: 15_000,
  });
  await page.getByLabel("Server name").fill("e2e-journey");
  await page.getByRole("button", { name: "Build server" }).click();
  await expect(page.getByText(/already exists/)).toBeVisible();
  await page.locator("aside").getByRole("button", { name: "Dashboard" }).click();

  // --- open detail; EULA gate is shown for a fresh server -----------------
  await page
    .locator("tr", { hasText: "e2e-journey" })
    .getByRole("button", { name: "e2e-journey" })
    .click();
  await expect(page.getByText(/accept the/i)).toBeVisible();

  // --- Mods tab: browse real Modrinth, install Fabric API -----------------
  await page.getByRole("button", { name: "Mods", exact: true }).click();
  await expect(page.getByText("No mods installed yet")).toBeVisible();

  await page.getByRole("button", { name: "Add mods" }).click();
  // Scope everything to the browser popup — once the install lands, the
  // installed list behind it also gains a "Fabric API" <li>.
  const browser = page.getByTestId("content-browser");
  await expect(browser.getByRole("button", { name: "Modrinth" })).toBeVisible();
  await browser.getByPlaceholder("Search mods…").fill("fabric api");
  const fabricApiRow = browser.locator("li", { hasText: "Fabric API" }).first();
  await expect(fabricApiRow).toBeVisible({ timeout: 20_000 });
  await fabricApiRow.getByRole("button", { name: "Install" }).click();

  // Success is reported inside the popup, and the hit flips to "Installed".
  await expect(browser.getByText(/^Installed: Fabric API/)).toBeVisible({
    timeout: 60_000,
  });
  await expect(
    fabricApiRow.getByRole("button", { name: "Installed" }),
  ).toBeVisible();
  await browser.getByRole("button", { name: "✕ Close" }).click();

  // Installed list shows it; the mod row exposes disable/remove.
  const modRow = page.locator("li", { hasText: "Fabric API" });
  await expect(modRow).toBeVisible();

  await modRow.getByRole("button", { name: "Disable" }).click();
  await expect(modRow.getByRole("button", { name: "Enable" })).toBeVisible();
  await modRow.getByRole("button", { name: "Enable" }).click();
  await expect(modRow.getByRole("button", { name: "Disable" })).toBeVisible();

  // Freshly installed → no updates.
  await page.getByRole("button", { name: "Check updates" }).click();
  await expect(page.getByText("Everything is up to date.")).toBeVisible({
    timeout: 30_000,
  });

  await modRow.getByRole("button", { name: "Remove" }).click();
  await expect(page.getByText("No mods installed yet")).toBeVisible();

  // --- Resource Packs tab: upload via the browser, offer in game, remove --
  await page.getByRole("button", { name: "Resource packs" }).click();
  await expect(page.getByText("No resource packs yet")).toBeVisible();
  await page.getByRole("button", { name: "Add packs" }).click();
  const rpBrowser = page.getByTestId("content-browser");
  await rpBrowser.getByRole("button", { name: "Upload" }).click();
  const fileChooserPromise = page.waitForEvent("filechooser");
  await rpBrowser.getByRole("button", { name: "Choose zip file…" }).click();
  await (await fileChooserPromise).setFiles("e2e/fixtures/e2e-pack.zip");
  await expect(rpBrowser.getByText(/^Uploaded: E2E test pack/)).toBeVisible({
    timeout: 15_000,
  });
  await rpBrowser.getByRole("button", { name: "✕ Close" }).click();

  const packRow = page.locator("li", { hasText: "E2E test pack" });
  await expect(packRow).toBeVisible({ timeout: 15_000 });

  // Point the server's resource-pack prompt at it (writes server.properties).
  await packRow.getByRole("button", { name: "Use in game" }).click();
  await expect(page.getByText("offered in game")).toBeVisible({ timeout: 15_000 });
  await packRow.getByRole("button", { name: "Stop offering" }).click();
  await expect(page.getByText("offered in game")).not.toBeVisible({
    timeout: 15_000,
  });

  await packRow.getByRole("button", { name: "Remove" }).click();
  await expect(page.getByText("No resource packs yet")).toBeVisible();

  // --- Datapacks tab: upload a datapack zip, toggle, remove ----------------
  await page.getByRole("button", { name: "Datapacks", exact: true }).click();
  await expect(page.getByText("No datapacks yet")).toBeVisible();
  await page.getByRole("button", { name: "Add datapacks" }).click();
  const dpBrowser = page.getByTestId("content-browser");
  await dpBrowser.getByRole("button", { name: "Upload" }).click();
  const dpChooser = page.waitForEvent("filechooser");
  await dpBrowser.getByRole("button", { name: "Choose zip file…" }).click();
  await (await dpChooser).setFiles("e2e/fixtures/e2e-pack.zip");
  await expect(dpBrowser.getByText(/^Uploaded: E2E test pack/)).toBeVisible({
    timeout: 15_000,
  });
  await dpBrowser.getByRole("button", { name: "✕ Close" }).click();

  const dpRow = page.locator("li", { hasText: "E2E test pack" });
  await expect(dpRow).toBeVisible();
  await dpRow.getByRole("button", { name: "Disable" }).click();
  await expect(dpRow.getByRole("button", { name: "Enable" })).toBeVisible();
  await dpRow.getByRole("button", { name: "Remove" }).click();
  await expect(page.getByText("No datapacks yet")).toBeVisible();

  // --- Backups tab: create, restore (confirm), delete ----------------------
  await page.getByRole("button", { name: "Backups", exact: true }).click();
  await expect(page.getByText("No backups yet")).toBeVisible();
  await page.getByRole("button", { name: "Create backup" }).click();
  await expect(page.getByText(/^Backup created/)).toBeVisible({ timeout: 30_000 });
  const backupRow = page.locator("li").filter({ hasText: ".zip" }).first();
  await expect(backupRow).toBeVisible();
  page.once("dialog", (d) => d.accept());
  await backupRow.getByRole("button", { name: "Restore" }).click();
  await expect(page.getByText("Backup restored")).toBeVisible({ timeout: 30_000 });
  await backupRow.getByRole("button", { name: "Delete" }).click();
  await expect(page.getByText("No backups yet")).toBeVisible();

  // --- Properties tab: edit a property and persist it ---------------------
  await page.getByRole("button", { name: "Properties" }).click();
  // Two Save buttons on this tab (settings vs server.properties), and the
  // sections nest — .last() picks the innermost (server.properties) match.
  const propsSection = page
    .locator("section", { hasText: "server.properties" })
    .last();
  const motd = propsSection.getByLabel("motd", { exact: true });
  await expect(motd).toBeVisible({ timeout: 15_000 });
  await motd.fill("e2e was here");
  await propsSection.getByRole("button", { name: "Save" }).click();
  await expect(propsSection.getByText("unsaved changes")).not.toBeVisible();

  // Reload drops the state-based routing back to the dashboard — re-open the
  // server from the sidebar and confirm the value came back from the file.
  await page.reload();
  await page
    .locator("aside")
    .getByRole("button", { name: "e2e-journey" })
    .click();
  await page.getByRole("button", { name: "Properties" }).click();
  await expect(
    page
      .locator("section", { hasText: "server.properties" })
      .last()
      .getByLabel("motd", { exact: true }),
  ).toHaveValue("e2e was here", { timeout: 15_000 });

  // --- delete from the detail page (confirm dialog) ------------------------
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Delete server" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await expect(page.getByText("Welcome to Lectern!")).toBeVisible();
});
