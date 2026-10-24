import { afterEach, describe, expect, it, vi } from "vitest";
import { addToList, listAddBody } from "./players";

describe("listAddBody", () => {
  it("sends only the uuid without options", () => {
    expect(listAddBody("abc")).toEqual({ uuid: "abc" });
    expect(listAddBody("abc", {})).toEqual({ uuid: "abc" });
  });

  it("leaves out blank options", () => {
    expect(listAddBody("abc", { reason: "   ", expires: "" })).toEqual({ uuid: "abc" });
  });

  it("carries a trimmed reason and the expiry when given", () => {
    expect(
      listAddBody("abc", { reason: "  griefing ", expires: "2030-01-01T00:00:00Z" }),
    ).toEqual({ uuid: "abc", reason: "griefing", expires: "2030-01-01T00:00:00Z" });
  });
});

describe("addToList", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stubFetch() {
    const lists = { whitelist: [], ops: [], banned: [{ uuid: "abc", name: "Notch" }] };
    const fetchMock = vi.fn(
      async (_url: string, _init: RequestInit) =>
        new Response(JSON.stringify(lists), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    return { fetchMock, lists };
  }

  function sentBody(fetchMock: ReturnType<typeof stubFetch>["fetchMock"]): unknown {
    return JSON.parse(String(fetchMock.mock.calls[0][1].body));
  }

  it("posts the reason for a ban and returns the lists", async () => {
    const { fetchMock, lists } = stubFetch();
    await expect(addToList("srv", "banned", "abc", { reason: "griefing" })).resolves.toEqual(
      lists,
    );
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/servers/srv/playerlists/banned");
    expect(init.method).toBe("POST");
    expect(sentBody(fetchMock)).toEqual({ uuid: "abc", reason: "griefing" });
  });

  it("posts only the uuid when no options are given", async () => {
    const { fetchMock } = stubFetch();
    await addToList("srv", "whitelist", "abc");
    expect(sentBody(fetchMock)).toEqual({ uuid: "abc" });
  });
});
