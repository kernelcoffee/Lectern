// Fast smoke tests — real backend, real browser, no heavy downloads. The only
// network these touch is the catalog (Mojang/Fabric version lists via the
// backend, disk-cached in .e2e-data/cache between runs).

import { test, expect } from "@playwright/test";

test("app shell loads and reaches the backend", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Lectern" })).toBeVisible();
  // Health check round-trip through the Vite proxy.
  await expect(page.getByText(/backend v/)).toBeVisible();
  await expect(page.getByText("No servers yet. Create one below.")).toBeVisible();
});

test("create-server wizard walks type → version → loader → details", async ({
  page,
}) => {
  await page.goto("/");

  // Step 0 — types come from the backend registry.
  await expect(page.getByRole("button", { name: /Vanilla/ })).toBeVisible();
  await page.getByRole("button", { name: /Fabric/ }).click();

  // Step 1 — real Minecraft version list for Fabric.
  const versionSelect = page.locator("select");
  await expect(versionSelect).toBeVisible({ timeout: 15_000 });
  await versionSelect.selectOption("1.20.1");

  // Step 2 — loader builds for 1.20.1, newest preselected.
  await expect(page.getByText("Fabric loader build")).toBeVisible({
    timeout: 15_000,
  });
  const loader = await page.locator("select").inputValue();
  expect(loader).not.toBe("");
  await page.getByRole("button", { name: "Continue" }).click();

  // Step 3 — summary reflects the choices; submit is gated on a name.
  await expect(page.getByText(/Fabric · MC 1\.20\.1 · loader/)).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Create server" }),
  ).toBeDisabled();
  await page.getByLabel("Name").fill("smoke-test");
  await expect(
    page.getByRole("button", { name: "Create server" }),
  ).toBeEnabled();

  // Don't submit (that's the @full journey) — reset instead.
  await page.getByRole("button", { name: "Start over" }).click();
  await expect(page.getByText("Choose a server type")).toBeVisible();
});

test("vanilla path skips the loader step", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /Vanilla/ }).click();
  await expect(page.locator("select")).toBeVisible({ timeout: 15_000 });
  await page.locator("select").selectOption("1.20.1");
  // Straight to details — no loader step for vanilla.
  await expect(page.getByText("Vanilla · MC 1.20.1")).toBeVisible();
  await expect(page.getByText("Fabric loader build")).not.toBeVisible();
});
