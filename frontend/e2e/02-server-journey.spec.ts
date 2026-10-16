// @full — the real end-to-end journey, from the docs/functional.md §5.1 happy
// path: create a Fabric server on the New-server page, watch the dashboard
// follow the install, manage mods against real Modrinth, accept the EULA,
// start the server and stop it from the console (live status feed), restore
// a backup, edit a property, delete the server.
//
// Run with:  E2E_FULL=1 npx playwright test
// Network: Fabric server jar (~1 MB), Temurin JRE (~45 MB, cached in
// .e2e-data/java after the first run), Modrinth API + one mod jar.
// Port: the server really binds 25565 on the host, so nothing else (e.g. a
// local Minecraft server) may be listening there during the run.

import { crc32 } from "node:zlib";
import { test, expect } from "@playwright/test";

/** Minimal stored (uncompressed) zip writer — enough for a tiny test archive. */
function makeZip(entries: Record<string, string>): Buffer {
  const locals: Buffer[] = [];
  const centrals: Buffer[] = [];
  let offset = 0;
  for (const [name, text] of Object.entries(entries)) {
    const nameBuf = Buffer.from(name, "utf8");
    const data = Buffer.from(text, "utf8");
    const crc = crc32(data);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4); // version needed
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(data.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBuf.length, 26);
    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4); // version made by
    central.writeUInt16LE(20, 6); // version needed
    central.writeUInt32LE(crc, 16);
    central.writeUInt32LE(data.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(nameBuf.length, 28);
    central.writeUInt32LE(offset, 42);
    locals.push(local, nameBuf, data);
    centrals.push(central, nameBuf);
    offset += local.length + nameBuf.length + data.length;
  }
  const centralDir = Buffer.concat(centrals);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  const count = Object.keys(entries).length;
  end.writeUInt16LE(count, 8);
  end.writeUInt16LE(count, 10);
  end.writeUInt32LE(centralDir.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, centralDir, end]);
}

test("full server journey: create → mods → start/stop → backups → properties → delete @full", async ({
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
  // Accept it — the live-status leg below starts the server for real.
  await page.getByRole("button", { name: "Accept EULA" }).click();
  await expect(page.getByText(/accept the/i)).toBeHidden();

  // --- Mods tab: browse real Modrinth, install Fabric API -----------------
  await page.getByRole("button", { name: "Mods", exact: true }).click();
  await expect(page.getByText("No mods installed yet")).toBeVisible();

  await page.getByRole("button", { name: "Add mods" }).click();
  // Scope everything to the browser popup — once the install lands, the
  // installed list behind it also gains a "Fabric API" <li>.
  const browser = page.getByTestId("content-browser");
  await expect(browser.getByRole("button", { name: "Modrinth" })).toBeVisible();
  await browser.getByPlaceholder("Search mods…").fill("fabric api");
  // The query survives a type switch and the way back (F-C-10).
  await browser.getByRole("button", { name: "Resource packs" }).click();
  await expect(browser.getByPlaceholder("Search resource packs…")).toHaveValue(
    "fabric api",
  );
  await browser.getByRole("button", { name: "Mods", exact: true }).click();
  await expect(browser.getByPlaceholder("Search mods…")).toHaveValue("fabric api");
  const fabricApiRow = browser.locator("li", { hasText: "Fabric API" }).first();
  await expect(fabricApiRow).toBeVisible({ timeout: 20_000 });

  // Clicking a result row opens the details panel beside the results
  // (F-C-8/9): links, compatible versions with per-build Install; its ✕
  // closes it and leaves the browser open.
  await fabricApiRow.getByText("Fabric API", { exact: true }).click();
  const details = browser.getByTestId("project-details");
  await expect(details.getByRole("link", { name: /Modrinth page/ })).toBeVisible({
    timeout: 20_000,
  });
  await expect(details.getByRole("button", { name: "Install" }).first()).toBeVisible({
    timeout: 20_000,
  });
  await details.getByRole("button", { name: "Close details" }).click();
  await expect(details).toBeHidden();
  await expect(browser).toBeVisible();

  await fabricApiRow.getByRole("button", { name: "Install" }).click();

  // Success is reported inside the popup, and the hit flips to "Installed".
  await expect(browser.getByText(/^Installed: Fabric API/)).toBeVisible({
    timeout: 60_000,
  });
  await expect(
    fabricApiRow.getByRole("button", { name: "Installed" }),
  ).toBeVisible();
  await browser.getByRole("button", { name: "Close", exact: true }).click();

  // Installed list shows it; the mod row exposes disable/remove.
  const modRow = page.locator("li", { hasText: "Fabric API" });
  await expect(modRow).toBeVisible();

  // Details from the installed list open in a side panel: the installed
  // build is marked current and the item can be pinned; ✕ closes the panel.
  await modRow.getByRole("button", { name: "Details" }).click();
  const installedDetails = page.getByTestId("project-details");
  await expect(installedDetails.getByText("current", { exact: true })).toBeVisible({
    timeout: 20_000,
  });
  await expect(
    installedDetails.getByRole("button", { name: "Pin current version" }),
  ).toBeVisible();
  await installedDetails.getByRole("button", { name: "Close details" }).click();
  await expect(installedDetails).toBeHidden();

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
  await rpBrowser.getByRole("button", { name: "Close" }).click();

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
  await dpBrowser.getByRole("button", { name: "Close" }).click();

  const dpRow = page.locator("li", { hasText: "E2E test pack" });
  await expect(dpRow).toBeVisible();
  await dpRow.getByRole("button", { name: "Disable" }).click();
  await expect(dpRow.getByRole("button", { name: "Enable" })).toBeVisible();
  await dpRow.getByRole("button", { name: "Remove" }).click();
  await expect(page.getByText("No datapacks yet")).toBeVisible();

  // --- Backups tab: create, restore (confirm), delete, upload --------------
  await page.getByRole("button", { name: "Backups", exact: true }).click();
  await expect(page.getByText("No backups yet")).toBeVisible();
  await page.getByRole("button", { name: "Create backup" }).click();
  await expect(page.getByText(/^Backup created/)).toBeVisible({ timeout: 30_000 });
  const backupRow = page.locator("li").filter({ hasText: ".zip" }).first();
  await expect(backupRow).toBeVisible();

  // --- live status (F-SM-12): start for real, stop from the console --------
  // The chip, the Restore gate and the header Clone gate all follow the
  // /ws/status feed — no navigation or reload in between.
  const statusChip = page.getByTestId("status-chip");
  await page.getByRole("button", { name: "Start", exact: true }).click();
  await expect(statusChip).toHaveText("running", { timeout: 300_000 });
  await expect(backupRow.getByRole("button", { name: "Restore" })).toBeDisabled();
  await page.getByRole("button", { name: "Console", exact: true }).click();
  // Commands typed before the console socket opens are dropped — wait for it.
  await expect(page.getByText("console connected")).toBeVisible();
  const consoleInput = page.getByPlaceholder(/type a server command/);
  await consoleInput.fill("stop");
  await consoleInput.press("Enter");
  await expect(statusChip).toHaveText("stopped", { timeout: 120_000 });
  await expect(page.getByRole("button", { name: "Clone", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Backups", exact: true }).click();
  await expect(backupRow.getByRole("button", { name: "Restore" })).toBeEnabled();
  page.once("dialog", (d) => d.accept());
  await backupRow.getByRole("button", { name: "Restore" }).click();
  await expect(page.getByText("Backup restored")).toBeVisible({ timeout: 30_000 });
  await backupRow.getByRole("button", { name: "Delete" }).click();
  await expect(page.getByText("No backups yet")).toBeVisible();

  // Upload a whole-server archive (as if downloaded and edited offline).
  await page.getByTestId("backup-upload-input").setInputFiles({
    name: "edited-backup.zip",
    mimeType: "application/zip",
    buffer: makeZip({
      "server.properties": "motd=uploaded\n",
      "world/level.dat": "WORLD",
    }),
  });
  await expect(page.getByText(/^Backup uploaded/)).toBeVisible({ timeout: 30_000 });
  const uploadedRow = page.locator("li").filter({ hasText: "uploaded" }).first();
  await expect(uploadedRow).toBeVisible();
  await uploadedRow.getByRole("button", { name: "Delete" }).click();
  await expect(page.getByText("No backups yet")).toBeVisible();

  // --- Schedule tab: friendly builder ↔ APScheduler round-trip -------------
  await page.getByRole("button", { name: "Schedule", exact: true }).click();
  await expect(page.getByText("No schedules yet")).toBeVisible();

  await page.getByLabel("Do this").selectOption("backup");
  await page.getByLabel("How often").selectOption("weekly");
  await page.getByLabel("At", { exact: true }).fill("06:30");
  // Chips default to all seven days — leave only Tue & Thu selected.
  for (const day of ["Mon", "Wed", "Fri", "Sat", "Sun"]) {
    await page.getByRole("button", { name: day, exact: true }).click();
  }
  // The preview shows the generated cron: weekday NAMES, never numbers —
  // APScheduler maps 0→Monday, so a numeric day would fire on the wrong day.
  await expect(page.getByText("30 6 * * tue,thu")).toBeVisible();
  await page.getByRole("button", { name: "Add schedule" }).click();

  // A "next" run proves the backend parsed the names the same way.
  const schedRow = page.locator("li", { hasText: "Back up server" });
  await expect(schedRow).toBeVisible();
  await expect(schedRow.getByText(/On Tue, Thu at 06:30/)).toBeVisible();
  await expect(schedRow.getByText(/next /)).toBeVisible();

  // Edit: the friendly controls are re-seeded from the stored cron.
  await schedRow.getByRole("button", { name: "Edit" }).click();
  const editModal = page.locator("div.fixed", { hasText: "Edit schedule" });
  await expect(editModal.getByLabel("How often")).toHaveValue("weekly");
  await expect(editModal.getByLabel("At", { exact: true })).toHaveValue("06:30");
  await editModal.getByLabel("Do this").selectOption("restart");
  await editModal.getByLabel("At", { exact: true }).fill("07:00");
  await editModal.getByRole("button", { name: "Save changes" }).click();
  const editedRow = page.locator("li", { hasText: "Restart server" });
  await expect(editedRow.getByText(/On Tue, Thu at 07:00/)).toBeVisible();

  // Disable (next run disappears), then delete.
  await editedRow.getByRole("button", { name: "Disable" }).click();
  await expect(editedRow.getByText(/disabled/)).toBeVisible();
  await editedRow.getByRole("button", { name: "Delete" }).click();
  await expect(page.getByText("No schedules yet")).toBeVisible();

  // --- Files tab: browse, create/edit a file, rename, delete ---------------
  await page.getByRole("button", { name: "Files", exact: true }).click();
  // The real server dir lists — the installed server has its properties file.
  await expect(
    page.getByRole("button", { name: "server.properties" }),
  ).toBeVisible();

  // New file (named via the prompt dialog), then write to it in the editor.
  page.once("dialog", (d) => d.accept("e2e-note.txt"));
  await page.getByRole("button", { name: "New file" }).click();
  const fileBtn = page.getByRole("button", { name: "e2e-note.txt" });
  await expect(fileBtn).toBeVisible();

  await fileBtn.click();
  const editor = page.locator("div.fixed", { hasText: "e2e-note.txt" });
  await editor.getByRole("textbox").fill("hello from e2e");
  await expect(editor.getByText("unsaved")).toBeVisible();
  await editor.getByRole("button", { name: "Save", exact: true }).click();
  await expect(editor.getByText("unsaved")).not.toBeVisible();
  await page.keyboard.press("Escape");

  // Re-open: the content came back from disk.
  await fileBtn.click();
  await expect(editor.getByRole("textbox")).toHaveValue("hello from e2e");
  await page.keyboard.press("Escape");

  // Rename via the row menu, then delete (confirm dialog).
  const fileRow = page.locator("tr", { hasText: "e2e-note.txt" });
  await fileRow.getByRole("button", { name: "Actions" }).click();
  page.once("dialog", (d) => d.accept("e2e-renamed.txt"));
  await fileRow.getByRole("button", { name: "Rename" }).click();
  const renamedRow = page.locator("tr", { hasText: "e2e-renamed.txt" });
  await expect(renamedRow).toBeVisible();
  await renamedRow.getByRole("button", { name: "Actions" }).click();
  page.once("dialog", (d) => d.accept());
  await renamedRow.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(renamedRow).not.toBeVisible();

  // --- Properties tab: edit a property and persist it ---------------------
  await page.getByRole("button", { name: "Properties", exact: true }).click();
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
  await page.getByRole("button", { name: "Properties", exact: true }).click();
  await expect(
    page
      .locator("section", { hasText: "server.properties" })
      .last()
      .getByLabel("motd", { exact: true }),
  ).toHaveValue("e2e was here", { timeout: 15_000 });

  // --- clone: full copy under a free name/port, then delete the copy -------
  await page.getByRole("button", { name: "Clone", exact: true }).click();
  // Success navigates to the clone's detail page.
  await expect(
    page.getByRole("heading", { name: "e2e-journey copy" }),
  ).toBeVisible({ timeout: 60_000 });
  // The copy carried the source's server.properties edits.
  await page.getByRole("button", { name: "Properties", exact: true }).click();
  await expect(
    page
      .locator("section", { hasText: "server.properties" })
      .last()
      .getByLabel("motd", { exact: true }),
  ).toHaveValue("e2e was here", { timeout: 15_000 });

  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Delete server" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();

  // Back to the original for the final teardown. NB: the sidebar button's
  // accessible name includes the status dot's title ("stopped e2e-journey"),
  // so this must NOT be an exact match — the clone is already gone, so the
  // substring is unambiguous.
  await page
    .locator("aside")
    .getByRole("button", { name: "e2e-journey" })
    .click();
  await expect(page.getByRole("heading", { name: "e2e-journey" })).toBeVisible();

  // --- delete from the detail page (confirm dialog) ------------------------
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Delete server" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await expect(page.getByText("Welcome to Lectern!")).toBeVisible();
});
