import { describe, expect, it } from "vitest";
import type { Server } from "../api/servers";
import { patchServerStatuses, reduceStatuses } from "./useStatusSocket";

function server(id: string, status: Server["status"]): Server {
  return {
    id,
    name: `srv-${id}`,
    type: "fabric",
    mc_version: "1.21",
    loader_version: null,
    port: 25565,
    memory_mb: 2048,
    status,
    created_at: "2026-01-01T00:00:00Z",
  };
}

describe("reduceStatuses", () => {
  it("replaces the map on a snapshot, dropping stale ids", () => {
    const next = reduceStatuses(
      { gone: "running", a: "running" },
      { type: "snapshot", servers: [{ server_id: "a", status: "stopped" }] },
    );
    expect(next).toEqual({ a: "stopped" });
  });

  it("merges one key on a status update", () => {
    const prev = { a: "stopped", b: "running" } as const;
    const next = reduceStatuses(prev, { type: "status", server_id: "a", status: "starting" });
    expect(next).toEqual({ a: "starting", b: "running" });
    expect(prev.a).toBe("stopped");
  });

  it("returns the same object when nothing changed", () => {
    const prev = { a: "stopped" } as const;
    expect(reduceStatuses(prev, { type: "status", server_id: "a", status: "stopped" })).toBe(prev);
    expect(
      reduceStatuses(prev, {
        type: "snapshot",
        servers: [{ server_id: "a", status: "stopped" }],
      }),
    ).toBe(prev);
  });
});

describe("patchServerStatuses", () => {
  it("returns the same array when no row differs", () => {
    const list = [server("a", "stopped"), server("b", "running")];
    expect(patchServerStatuses(list, { a: "stopped", b: "running" })).toBe(list);
    expect(patchServerStatuses(list, {})).toBe(list);
  });

  it("replaces only the changed row and keeps its other fields", () => {
    const list = [server("a", "stopped"), server("b", "running")];
    const next = patchServerStatuses(list, { a: "running", b: "running" });
    expect(next).not.toBe(list);
    expect(next[0]).not.toBe(list[0]);
    expect(next[0]).toEqual({ ...list[0], status: "running" });
    expect(next[1]).toBe(list[1]);
  });

  it("ignores ids that are not in the list", () => {
    const list = [server("a", "stopped")];
    expect(patchServerStatuses(list, { zzz: "running" })).toBe(list);
  });
});
