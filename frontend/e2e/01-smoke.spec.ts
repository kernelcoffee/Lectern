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

test("create page: security, seed and world-import sections", async ({
  page,
}) => {
  await page.goto("/");
  await page.locator("aside").getByRole("button", { name: "New server" }).click();

  // Whitelist is on by default (secure by default).
  const whitelist = page.getByRole("checkbox", { name: /Enable whitelist/ });
  await expect(whitelist).toBeChecked();

  // Seed is free-form while generating a new world…
  const seed = page.getByLabel(/World seed/);
  await expect(seed).toBeEnabled();
  await seed.fill("e2e-seed");

  // …but ignored (disabled) when importing a world; the skip-files filter
  // appears with the Distant Horizons default, and the submit label flips.
  await page.getByLabel("World source").selectOption("upload");
  await expect(seed).toBeDisabled();
  await expect(page.getByLabel(/Skip files/)).toHaveValue("*DistantHorizons*");
  await expect(
    page.getByRole("button", { name: "Build server + import world" }),
  ).toBeVisible();

  // Back to a fresh world: seed usable again, plain submit label.
  await page.getByLabel("World source").selectOption("none");
  await expect(seed).toBeEnabled();
  await expect(page.getByRole("button", { name: "Build server" })).toBeVisible();
});

test("create page: the Proxy kind is explicit and separate", async ({ page }) => {
  await page.goto("/");
  await page.locator("aside").getByRole("button", { name: "New server" }).click();

  // Game server is the default; Velocity is NOT in the game-type dropdown.
  const kindSwitch = page.getByRole("radiogroup", { name: "What to create" });
  await expect(kindSwitch.getByRole("radio", { name: /Game server/ })).toBeChecked();
  await expect(page.getByLabel("Server type")).toBeVisible();
  await expect(
    page.getByLabel("Server type").locator('option[value="velocity"]'),
  ).toHaveCount(0);

  // Switching to Proxy: fixed software, proxy version list, no MC-only
  // sections, and a proxy-specific submit label.
  await kindSwitch.getByRole("radio", { name: /Proxy/ }).click();
  await expect(page.getByRole("heading", { name: "New proxy" })).toBeVisible();
  // Kind-aware prefill: proxies get a proxy name and the public port.
  await expect(page.getByLabel("Proxy name")).toHaveValue("New proxy", {
    timeout: 10_000,
  });
  await expect(page.getByText("Proxy software")).toBeVisible();
  await expect(page.getByText("Velocity", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Velocity version")).not.toHaveValue("", {
    timeout: 15_000,
  });
  await expect(page.getByLabel(/World seed/)).not.toBeVisible();
  await expect(page.getByRole("checkbox", { name: /Enable whitelist/ })).not.toBeVisible();
  await expect(page.getByRole("button", { name: "Build proxy" })).toBeEnabled();

  // And back: the game flow is intact.
  await kindSwitch.getByRole("radio", { name: /Game server/ }).click();
  await expect(page.getByLabel("Server type")).toHaveValue("vanilla", {
    timeout: 15_000,
  });
  await expect(page.getByRole("button", { name: "Build server" })).toBeVisible();
});

test("settings: edit a tunable, save, and see it drive the create form", async ({
  page,
}) => {
  await page.goto("/");
  await page.locator("aside").getByRole("button", { name: "Settings" }).click();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();

  // Tunables render grouped with bounds; save is gated on a real change.
  const memory = page.getByLabel("Default server memory");
  await expect(memory).not.toHaveValue("", { timeout: 10_000 });
  const original = await memory.inputValue();
  const save = page.getByRole("button", { name: "Save changes" });
  await expect(save).toBeDisabled();

  await memory.fill("3072");
  await expect(save).toBeEnabled();
  await save.click();
  await expect(page.getByText("Settings saved.")).toBeVisible();

  // The create form prefills memory from the stored setting (via /suggest).
  await page.locator("aside").getByRole("button", { name: "New server" }).click();
  await expect(page.getByLabel("Memory (MB)")).toHaveValue("3072", {
    timeout: 10_000,
  });

  // Restore the original so this test doesn't leak into later specs.
  await page.locator("aside").getByRole("button", { name: "Settings" }).click();
  await page.getByLabel("Default server memory").fill(original);
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByText("Settings saved.")).toBeVisible();
});
