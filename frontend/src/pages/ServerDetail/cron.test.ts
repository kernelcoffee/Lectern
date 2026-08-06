// cron.ts is the one place where a silent bug bites weeks later (a schedule
// firing on the wrong day), so every emitted shape and every fallback is
// pinned here. The overriding invariant: weekdays are NAMES (mon…sun), never
// numbers — the backend's APScheduler maps 0→Monday, not the Unix 0→Sunday.

import { describe, expect, it } from "vitest";
import {
  buildCron,
  CronBuilderState,
  DEFAULT_BUILDER,
  describeCron,
  parseCron,
} from "./cron";

function state(over: Partial<CronBuilderState>): CronBuilderState {
  return { ...DEFAULT_BUILDER, ...over };
}

describe("buildCron", () => {
  it("daily uses the picked time", () => {
    expect(buildCron(state({ frequency: "daily", time: "04:30" }))).toBe("30 4 * * *");
    expect(buildCron(state({ frequency: "daily", time: "00:00" }))).toBe("0 0 * * *");
  });

  it("daily tolerates a malformed time (falls back to 00:00 fields)", () => {
    expect(buildCron(state({ frequency: "daily", time: "" }))).toBe("0 0 * * *");
  });

  it("weekly emits day NAMES, Monday-first, deduped", () => {
    expect(
      buildCron(state({ frequency: "weekly", time: "06:15", weekdays: ["thu", "tue", "thu"] })),
    ).toBe("15 6 * * tue,thu");
  });

  it("weekly with no days is invalid (null)", () => {
    expect(buildCron(state({ frequency: "weekly", weekdays: [] }))).toBeNull();
  });

  it("weekly with all 7 days collapses to the daily form", () => {
    expect(
      buildCron(
        state({
          frequency: "weekly",
          time: "04:00",
          weekdays: ["sun", "sat", "fri", "thu", "wed", "tue", "mon"],
        }),
      ),
    ).toBe("0 4 * * *");
  });

  it("never emits numeric weekdays (APScheduler 0=Monday mismatch)", () => {
    const cron = buildCron(
      state({ frequency: "weekly", time: "04:00", weekdays: ["sun", "mon"] }),
    )!;
    expect(cron.split(/\s+/)[4]).toBe("mon,sun");
    expect(cron).not.toMatch(/[*\s][0-7](,[0-7])*$/);
  });

  it("hourly clamps the minute into 0–59", () => {
    expect(buildCron(state({ frequency: "hourly", minute: 45 }))).toBe("45 * * * *");
    expect(buildCron(state({ frequency: "hourly", minute: 99 }))).toBe("59 * * * *");
    expect(buildCron(state({ frequency: "hourly", minute: -5 }))).toBe("0 * * * *");
  });

  it("everyNHours clamps N into 1–23", () => {
    expect(buildCron(state({ frequency: "everyNHours", everyN: 6 }))).toBe("0 */6 * * *");
    expect(buildCron(state({ frequency: "everyNHours", everyN: 0 }))).toBe("0 */1 * * *");
    expect(buildCron(state({ frequency: "everyNHours", everyN: 48 }))).toBe("0 */23 * * *");
  });

  it("advanced passes the raw expression through, trimmed; empty is null", () => {
    expect(buildCron(state({ frequency: "advanced", raw: "  5 3 1 * *  " }))).toBe("5 3 1 * *");
    expect(buildCron(state({ frequency: "advanced", raw: "   " }))).toBeNull();
  });
});

describe("describeCron", () => {
  it("describes the shapes the builder emits", () => {
    expect(describeCron("0 4 * * *")).toBe("Every day at 04:00");
    expect(describeCron("30 18 * * *")).toBe("Every day at 18:30");
    expect(describeCron("0 * * * *")).toBe("Every hour");
    expect(describeCron("15 * * * *")).toBe("Every hour at :15");
    expect(describeCron("0 */6 * * *")).toBe("Every 6 hours");
    expect(describeCron("0 4 * * tue,thu")).toBe("On Tue, Thu at 04:00");
  });

  it("labels the weekday/weekend shorthands", () => {
    expect(describeCron("0 4 * * mon,tue,wed,thu,fri")).toBe("Every weekday at 04:00");
    expect(describeCron("0 4 * * sat,sun")).toBe("On weekends at 04:00");
  });

  it("orders unsorted day lists Monday-first", () => {
    expect(describeCron("0 4 * * sun,mon")).toBe("On Mon, Sun at 04:00");
  });

  it("falls back to the raw expression for anything it did not emit", () => {
    for (const raw of [
      "0 4 1 * *", // day-of-month
      "0 4 * 6 *", // month
      "*/5 * * * *", // step minutes
      "0 4 * * 1", // numeric weekday — deliberately unsupported
      "0 4 * * mon-fri", // range
      "not a cron",
      "1 2 3 4", // wrong field count
    ]) {
      expect(describeCron(raw)).toBe(raw);
    }
  });
});

describe("parseCron", () => {
  it("recovers builder state for every emitted shape", () => {
    expect(parseCron("30 4 * * *")).toMatchObject({ frequency: "daily", time: "04:30" });
    expect(parseCron("15 * * * *")).toMatchObject({ frequency: "hourly", minute: 15 });
    expect(parseCron("0 */6 * * *")).toMatchObject({ frequency: "everyNHours", everyN: 6 });
    expect(parseCron("0 6 * * tue,thu")).toMatchObject({
      frequency: "weekly",
      time: "06:00",
      weekdays: ["tue", "thu"],
    });
  });

  it("keeps `raw` as the incoming expression so Advanced shows current state", () => {
    expect(parseCron("30 4 * * *").raw).toBe("30 4 * * *");
  });

  it("falls back to advanced (expression intact) for hand-written crontabs", () => {
    for (const raw of [
      "0 4 1 * *", // day-of-month set
      "0 4 * 6 *", // month set
      "*/5 * * * *", // step minutes
      "0 4 * * 1", // numeric weekday
      "0 4 * * mon-fri", // range
      "99 4 * * *", // minute out of range
      "0 99 * * *", // hour out of range
      "0 */0 * * *", // step out of range
      "garbage",
    ]) {
      const s = parseCron(raw);
      expect(s.frequency).toBe("advanced");
      expect(s.raw).toBe(raw);
    }
  });

  it("round-trips: buildCron(parseCron(x)) === x for emitted shapes", () => {
    for (const cron of [
      "0 4 * * *",
      "30 18 * * *",
      "15 * * * *",
      "0 */6 * * *",
      "0 6 * * tue,thu",
      "0 4 * * mon,tue,wed,thu,fri",
      "0 4 * * sat,sun",
    ]) {
      expect(buildCron(parseCron(cron))).toBe(cron);
    }
  });

  it("round-trips a hand-written crontab through advanced unchanged", () => {
    expect(buildCron(parseCron("*/10 2-4 1,15 * *"))).toBe("*/10 2-4 1,15 * *");
  });
});
