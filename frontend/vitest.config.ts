// Unit tests only — Playwright owns e2e/*.spec.ts, so scope vitest to src/.
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
