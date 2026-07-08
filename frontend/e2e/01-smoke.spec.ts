// Fast smoke tests — real backend, real browser, no heavy downloads. The only
// network these touch is the catalog (Mojang/Fabric version lists via the
// backend, disk-cached in .e2e-data/cache between runs).

import { test, expect } from "@playwright/test";

test("app shell: sidebar, dashboard default, backend health", async ({
  page,
}) => {
  await page.goto("/");
  const sidebar = page.locator("aside");
  await expect(sidebar.getByRole("heading", { name: "Lectern" })).toBeVisible();
  await expect(sidebar.getByText(/backend v/)).toBeVisible();
  await expect(sidebar.getByRole("button", { name: "Dashboard" })).toBeVisible();
  await expect(sidebar.getByRole("button", { name: "New server" })).toBeVisible();

  // Dashboard is the default view: stat tiles + empty-state welcome.
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await expect(page.getByText("Servers running")).toBeVisible();
  await expect(page.getByText("0 / 0")).toBeVisible();
  await expect(page.getByText("Welcome to Lectern!")).toBeVisible();
});

test("create page: fabric form loads real catalog and gates submit", async ({
  page,
}) => {
  await page.goto("/");
  await page.locator("aside").getByRole("button", { name: "New server" }).click();
  await expect(page.getByRole("heading", { name: "New server" })).toBeVisible();

  // Type pills — vanilla preselected, switch to Fabric.
  await page.getByRole("button", { name: /Fabric/ }).click();

  // Real Minecraft version list for Fabric, then loader builds for 1.20.1.
  const version = page.getByLabel("Minecraft version");
  await expect(version).toBeVisible({ timeout: 15_000 });
  await version.selectOption("1.20.1");
  const loader = page.getByLabel("Fabric loader build");
  await expect(loader).toBeVisible({ timeout: 15_000 });
  await expect(loader).not.toHaveValue("", { timeout: 15_000 });

  // Build is gated on a name.
  await expect(page.getByRole("button", { name: "Build server" })).toBeDisabled();
  await page.getByLabel("Server name").fill("smoke-test");
  await expect(page.getByRole("button", { name: "Build server" })).toBeEnabled();

  // Reset clears the form (don't submit — that's the @full journey).
  await page.getByRole("button", { name: "Reset" }).click();
  await expect(page.getByRole("button", { name: "Build server" })).toBeDisabled();
});

test("vanilla type has no loader field", async ({ page }) => {
  await page.goto("/");
  await page.locator("aside").getByRole("button", { name: "New server" }).click();
  // Vanilla is preselected (first type); its version list loads.
  await expect(page.getByLabel("Minecraft version")).toBeVisible({
    timeout: 15_000,
  });
  await page.getByLabel("Minecraft version").selectOption("1.20.1");
  await expect(page.getByLabel("Fabric loader build")).not.toBeVisible();
});
