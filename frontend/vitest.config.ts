// Unit tests only — Playwright owns e2e/*.spec.ts, so scope vitest to src/.
// Coverage is reported for the logic modules vitest can see (lib, hooks, api);
// .tsx components are exercised by Playwright, which produces no coverage.
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["src/**/*.test.{ts,tsx}"],
    // Node by default (fast, pure modules). A test that renders a hook or a
    // component opts in with `// @vitest-environment jsdom` at the top and
    // uses renderHook/render from @testing-library/react.
    environment: "node",
    coverage: {
      provider: "v8",
      include: ["src/lib/**/*.ts", "src/hooks/**/*.ts", "src/api/**/*.ts"],
      exclude: ["**/*.test.ts"],
      reporter: ["text-summary", "cobertura"],
      reportsDirectory: "coverage",
    },
  },
});
