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

test("create page: everything preselected, fabric via dropdown, submit gating", async ({
  page,
}) => {
  await page.goto("/");
  await page.locator("aside").getByRole("button", { name: "New server" }).click();
  await expect(page.getByRole("heading", { name: "New server" })).toBeVisible();

  // Name/port prefilled with the first free suggestion; type defaults to
  // vanilla and the latest stable version is preselected — a fresh page is
  // submittable as-is.
  await expect(page.getByLabel("Server name")).toHaveValue("New server", {
    timeout: 10_000,
  });
  await expect(page.getByLabel("Server port")).toHaveValue("25565");
  await expect(page.getByLabel("Server type")).toHaveValue("vanilla", {
    timeout: 15_000,
  });
  await expect(page.getByLabel("Minecraft version")).not.toHaveValue("", {
    timeout: 15_000,
  });
  await expect(page.getByRole("button", { name: "Build server" })).toBeEnabled();

  // Switch to Fabric via the dropdown: versions reload (latest preselected
  // again) and the newest loader build comes prefilled.
  await page.getByLabel("Server type").selectOption("fabric");
  await expect(page.getByLabel("Minecraft version")).not.toHaveValue("", {
    timeout: 15_000,
  });
  const loader = page.getByLabel("Fabric loader build");
  await expect(loader).toBeVisible({ timeout: 15_000 });
  await expect(loader).not.toHaveValue("", { timeout: 15_000 });
  // Older versions remain selectable.
  await page.getByLabel("Minecraft version").selectOption("1.20.1");
  await expect(loader).not.toHaveValue("", { timeout: 15_000 });
  await expect(page.getByRole("button", { name: "Build server" })).toBeEnabled();

  // Submit is gated on a non-empty name.
  await page.getByLabel("Server name").fill("");
  await expect(page.getByRole("button", { name: "Build server" })).toBeDisabled();

  // Reset re-suggests the name and re-preselects the latest version (don't
  // submit — that's the @full journey).
  await page.getByRole("button", { name: "Reset" }).click();
  await expect(page.getByLabel("Server name")).toHaveValue("New server", {
    timeout: 10_000,
  });
  await expect(page.getByLabel("Minecraft version")).not.toHaveValue("", {
    timeout: 15_000,
  });
  await expect(page.getByRole("button", { name: "Build server" })).toBeEnabled();
});

test("vanilla type has no loader field", async ({ page }) => {
  await page.goto("/");
  await page.locator("aside").getByRole("button", { name: "New server" }).click();
  // Vanilla is the default type; latest version preselected, no loader field.
  await expect(page.getByLabel("Server type")).toHaveValue("vanilla", {
    timeout: 15_000,
  });
  await expect(page.getByLabel("Minecraft version")).not.toHaveValue("", {
    timeout: 15_000,
  });
  await expect(page.getByLabel("Fabric loader build")).not.toBeVisible();
});
