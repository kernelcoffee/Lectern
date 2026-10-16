import { describe, expect, it } from "vitest";
import { formatBytes, parseUtc } from "./format";

describe("parseUtc", () => {
  const expected = Date.UTC(2026, 9, 16, 16, 30, 41, 768);

  it("treats a naive timestamp as UTC", () => {
    expect(parseUtc("2026-10-16T16:30:41.768635").getTime()).toBe(expected);
  });

  it("accepts a timestamp that already ends in Z", () => {
    expect(parseUtc("2026-10-16T16:30:41.768635Z").getTime()).toBe(expected);
  });

  it("accepts a numeric offset", () => {
    expect(parseUtc("2026-10-16T18:30:41.768635+02:00").getTime()).toBe(expected);
  });

  it("never yields an invalid date for either form", () => {
    for (const iso of ["2026-10-16T16:30:41", "2026-10-16T16:30:41Z"]) {
      expect(Number.isNaN(parseUtc(iso).getTime())).toBe(false);
    }
  });
});

describe("formatBytes", () => {
  it("picks the unit by magnitude", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2 KB");
    expect(formatBytes(1.5 * (1 << 20))).toBe("1.5 MB");
  });
});
