// @full — the real end-to-end journey, from the docs/functional.md §5.1 happy
// path (minus starting Minecraft, which backend tests cover): create a Fabric
// server through the wizard, watch the install finish, manage mods against
// real Modrinth, edit a property, delete the server.
//
// Run with:  E2E_FULL=1 npx playwright test
// Network: Fabric server jar (~1 MB), Temurin JRE (~45 MB, cached in
// .e2e-data/java after the first run), Modrinth API + one mod jar.

import { test, expect } from "@playwright/test";

test("full server journey: create → mods → properties → delete @full", async ({
  page,
}) => {
  test.setTimeout(600_000);

  // --- create a Fabric 1.20.1 server through the wizard -------------------
  await page.goto("/");
  await page.getByRole("button", { name: /Fabric/ }).click();
  await page.locator("select").selectOption("1.20.1", { timeout: 15_000 });
  await page.getByRole("button", { name: "Continue" }).click();
  await page.getByLabel("Name").fill("e2e-journey");
  await page.getByRole("button", { name: "Create server" }).click();

  // The list shows the row installing, then flips to stopped (jar + JRE).
  const row = page.locator("li", { hasText: "e2e-journey" });
  await expect(row).toBeVisible();
  await expect(row.getByText("stopped")).toBeVisible({ timeout: 300_000 });

  // --- open detail; EULA gate is shown for a fresh server -----------------
  await row.getByRole("button", { name: /e2e-journey/ }).click();
  await expect(page.getByText(/accept the/i)).toBeVisible();

  // --- Mods tab: browse real Modrinth, install Fabric API -----------------
  await page.getByRole("button", { name: "Mods", exact: true }).click();
  await expect(page.getByText("No mods installed yet")).toBeVisible();

  await page.getByRole("button", { name: "Browse Modrinth" }).click();
  // Scope everything to the modal — once the install lands, the installed
  // list behind it also gains a "Fabric API" <li>.
  const modal = page.getByTestId("browse-modal");
  await modal.getByPlaceholder("Search mods…").fill("fabric api");
  const fabricApiRow = modal.locator("li", { hasText: "Fabric API" }).first();
  await expect(fabricApiRow).toBeVisible({ timeout: 20_000 });
  await fabricApiRow.getByRole("button", { name: "Install" }).click();

  // Success is reported inside the modal, and the hit flips to "Installed".
  await expect(modal.getByText(/^Installed: Fabric API/)).toBeVisible({
    timeout: 60_000,
  });
  await expect(
    fabricApiRow.getByRole("button", { name: "Installed" }),
  ).toBeVisible();
  await modal.getByRole("button", { name: "✕ Close" }).click();

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

  // Reload drops the state-based routing back to the list — re-open the
  // server and confirm the value came back from the file on disk.
  await page.reload();
  await page.getByRole("button", { name: /e2e-journey/ }).click();
  await page.getByRole("button", { name: "Properties" }).click();
  await expect(
    page
      .locator("section", { hasText: "server.properties" })
      .last()
      .getByLabel("motd", { exact: true }),
  ).toHaveValue("e2e was here", { timeout: 15_000 });

  // --- delete the server from the list -------------------------------------
  await page.getByRole("button", { name: "← Back to servers" }).click();
  await page
    .locator("li", { hasText: "e2e-journey" })
    .getByRole("button", { name: "Delete" })
    .click();
  await expect(page.getByText("No servers yet. Create one below.")).toBeVisible();
});
