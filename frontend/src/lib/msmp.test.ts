import { describe, expect, it } from "vitest";
import {
  MSMP_CONNECTED_POLL_MS,
  MSMP_PENDING_POLL_MS,
  msmpChip,
  msmpPollInterval,
  rosterSource,
} from "./msmp";

describe("msmpChip", () => {
  it("shows nothing when the server has no client", () => {
    expect(msmpChip(null)).toBeNull();
    expect(msmpChip(undefined)).toBeNull();
  });

  it("names the schema version as a schema, only in the title", () => {
    const chip = msmpChip({ connected: true, schema_version: "3.1.0" });
    expect(chip?.connected).toBe(true);
    expect(chip?.label).toBe("management protocol");
    expect(chip?.label).not.toContain("3.1.0");
    expect(chip?.title).toContain("(schema 3.1.0)");
  });

  it("omits the schema when rpc.discover has not answered", () => {
    const chip = msmpChip({ connected: true, schema_version: null });
    expect(chip?.title).not.toContain("schema");
  });

  it("shows a pending chip explaining the console fallback when disconnected", () => {
    const chip = msmpChip({ connected: false, schema_version: "3.1.0" });
    expect(chip?.connected).toBe(false);
    expect(chip?.label).toBe("management protocol connecting…");
    expect(chip?.title).toContain("use the server console until it connects");
  });
});

describe("rosterSource", () => {
  it("names the management protocol only while connected", () => {
    expect(rosterSource({ connected: true, schema_version: "3.1.0" })).toContain(
      "management protocol",
    );
    expect(rosterSource({ connected: false, schema_version: null })).toContain("server console");
    expect(rosterSource(null)).toContain("server console");
  });
});

describe("msmpPollInterval", () => {
  it("does not poll without a running server that has a client", () => {
    expect(msmpPollInterval("running", null)).toBeNull();
    expect(msmpPollInterval("running", undefined)).toBeNull();
    expect(msmpPollInterval("stopped", { connected: true, schema_version: "3.1.0" })).toBeNull();
    expect(msmpPollInterval(undefined, { connected: false, schema_version: null })).toBeNull();
  });

  it("polls quickly while connecting", () => {
    expect(msmpPollInterval("running", { connected: false, schema_version: null })).toBe(
      MSMP_PENDING_POLL_MS,
    );
  });

  it("keeps polling once connected so a later disconnect is noticed", () => {
    expect(msmpPollInterval("running", { connected: true, schema_version: "3.1.0" })).toBe(
      MSMP_CONNECTED_POLL_MS,
    );
    expect(MSMP_CONNECTED_POLL_MS).toBeGreaterThan(MSMP_PENDING_POLL_MS);
  });
});
