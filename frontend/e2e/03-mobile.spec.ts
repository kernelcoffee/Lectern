// Phone-viewport smoke — guards the responsive round (2026-08): the sidebar
// is an off-canvas drawer behind a hamburger top bar below md, and no page
// may scroll horizontally. Same fast tier as 01 (catalog calls only).

import { test, expect, Page } from "@playwright/test";

test.use({ viewport: { width: 390, height: 844 } });

/** The page itself must never scroll sideways — wide content scrolls inside
 *  its own overflow-x-auto container instead. */
async function expectNoHorizontalScroll(page: Page, label: string) {
  const { scrollWidth, clientWidth } = await page.evaluate(() => ({
    scrollWidth: document.scrollingElement!.scrollWidth,
    clientWidth: document.scrollingElement!.clientWidth,
  }));
  expect(scrollWidth, `${label} scrolls horizontally`).toBeLessThanOrEqual(
    clientWidth,
  );
}

test("phone: sidebar is a drawer behind the hamburger", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();

  // Closed: the top bar shows, the drawer sits off-canvas to the left.
  const hamburger = page.getByRole("button", { name: "Open navigation" });
  await expect(hamburger).toBeVisible();
  const aside = page.locator("aside");
  await expect
    .poll(async () => (await aside.boundingBox())?.x ?? 0)
    .toBeLessThan(0);

  // Open: drawer slides in with a backdrop.
  await hamburger.click();
  await expect.poll(async () => (await aside.boundingBox())?.x).toBe(0);
  await expect(aside.getByText(/backend/)).toBeVisible();

  // Navigating from the drawer closes it and switches the page.
  await aside.getByRole("button", { name: "Players" }).click();
  await expect(page.getByRole("heading", { name: "Players" })).toBeVisible();
  await expect
    .poll(async () => (await aside.boundingBox())?.x ?? 0)
    .toBeLessThan(0);

  // Reopen and dismiss via the backdrop instead.
  await hamburger.click();
  await expect.poll(async () => (await aside.boundingBox())?.x).toBe(0);
  await page.mouse.click(380, 500); // outside the 240px drawer
  await expect
    .poll(async () => (await aside.boundingBox())?.x ?? 0)
    .toBeLessThan(0);
});

test("phone: dashboard and create page fit the viewport", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await expectNoHorizontalScroll(page, "dashboard");

  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.locator("aside").getByRole("button", { name: "New server" }).click();
  // Let the catalog load so the full form (all sections) is rendered.
  await expect(page.getByLabel("Minecraft version")).not.toHaveValue("", {
    timeout: 15_000,
  });
  await expectNoHorizontalScroll(page, "create");
});
