// Smoke tier — the Backups tab's upload UI (hidden picker → progress →
// toast → `uploaded` row, and the backend's error detail on a bad archive).
// No installed server exists in this tier, so the server and its backup
// endpoints are stubbed with page.route; everything else hits the real
// backend. Archive validation itself is covered by backend/tests/test_backups.py,
// and the @full journey (02) uploads against the real stack.

import { test, expect, Route } from "@playwright/test";

const ID = "e2e-stub-server";
const server = {
  id: ID,
  name: "Stubbed backups",
  type: "vanilla",
  mc_version: "1.21",
  loader_version: null,
  port: 25599,
  memory_mb: 1024,
  status: "stopped",
  created_at: "2026-01-01T00:00:00",
};
const detail = {
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
  running: false,
};

test("backups tab: upload an archive, then surface a rejected one", async ({ page }) => {
  const backups: object[] = [];
  let uploads = 0;
  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

  await page.route(
    (url) => url.pathname.startsWith("/api/servers"),
    async (route) => {
      const { pathname } = new URL(route.request().url());
      const method = route.request().method();
      if (pathname === "/api/servers" && method === "GET") return json(route, [server]);
      if (pathname === `/api/servers/${ID}` && method === "GET") return json(route, detail);
      if (pathname === `/api/servers/${ID}/backups` && method === "GET")
        return json(route, backups);
      if (pathname === `/api/servers/${ID}/backups/upload` && method === "POST") {
        uploads += 1;
        if (uploads > 1)
          return json(route, { detail: "Backup contains an unsafe path: '../evil'" }, 400);
        const row = {
          id: "b1",
          server_id: ID,
          filename: "20260101-000000-000000.zip",
          size_bytes: 2048,
          created_at: "2026-01-01T00:00:00",
          trigger: "uploaded",
        };
        backups.push(row);
        return json(route, row, 201);
      }
      return route.fallback();
    },
  );

  await page.goto("/");
  await page.locator("aside").getByRole("button", { name: /Stubbed backups/ }).click();
  await page.getByRole("button", { name: "Backups", exact: true }).click();
  await expect(page.getByText("No backups yet")).toBeVisible();
  await expect(page.getByRole("button", { name: "Upload backup" })).toBeEnabled();

  const input = page.getByTestId("backup-upload-input");
  const file = {
    name: "edited-backup.zip",
    mimeType: "application/zip",
    buffer: Buffer.from("PK stub — the backend is stubbed here"),
  };
  await input.setInputFiles(file);
  await expect(page.getByText("Backup uploaded (2 KB).")).toBeVisible();
  const row = page.locator("li").filter({ hasText: "20260101-000000-000000.zip" });
  await expect(row).toContainText("uploaded");
  await expect(page.getByRole("button", { name: "Upload backup" })).toBeEnabled();

  // A rejected archive: the backend's detail is toasted, the list is unchanged.
  await input.setInputFiles(file);
  await expect(page.getByText("Backup contains an unsafe path: '../evil'")).toBeVisible();
  await expect(page.locator("li").filter({ hasText: ".zip" })).toHaveCount(1);
  await expect(page.getByRole("button", { name: "Upload backup" })).toBeEnabled();
  expect(uploads).toBe(2);
});
