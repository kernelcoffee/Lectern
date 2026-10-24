// @vitest-environment jsdom
// The add picker of a player list: a list entry the server resolved itself
// (allowlist/add by name on an offline-mode or proxied backend) carries the
// server's UUID, not the registry's, so presence is matched by name as well,
// but only for entries whose UUID is not already a registry UUID.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Player, PlayerEntry } from "../../api/players";
import { ToastProvider } from "../../components/Toasts";
import PlayersTab from "./PlayersTab";

const NOTCH_REGISTRY = "069a79f444e94726a5befca90e38aaf5";
// UUID.nameUUIDFromBytes("OfflinePlayer:Notch"), what an offline server writes.
const NOTCH_OFFLINE = "b50ad385829d3141a2167e7d7539ba7f";
const JEB = "853c80ef3c3749fdaa49938b674adae6";

const registry: Player[] = [
  { uuid: NOTCH_REGISTRY, name: "Notch", added_at: "2026-01-01T00:00:00" },
  { uuid: JEB, name: "jeb_", added_at: "2026-01-01T00:00:00" },
];

function json(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function stubLists(whitelist: PlayerEntry[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/servers/srv/playerlists") return json({ whitelist, ops: [], banned: [] });
      if (url === "/api/players") return json(registry);
      if (url === "/api/servers/srv/players/online") return json([]);
      throw new Error(`unexpected fetch ${url}`);
    }),
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** The names the Whitelist picker offers, once the registry has loaded. */
async function offered(): Promise<string[]> {
  render(
    <ToastProvider>
      <PlayersTab serverId="srv" msmp={{ connected: true, schema_version: "3.1.0" }} />
    </ToastProvider>,
  );
  const picker = await waitFor(() => {
    const el = screen.getByLabelText("Player to add to Whitelist");
    // Wait until the registry is in: the placeholder plus at least one player.
    expect(el.querySelectorAll("option").length).toBeGreaterThan(1);
    return el;
  });
  return Array.from(picker.querySelectorAll("option"))
    .slice(1)
    .map((o) => o.textContent ?? "");
}

describe("PlayersTab add picker", () => {
  it("offers every registry player missing from the list", async () => {
    stubLists([]);
    expect(await offered()).toEqual(["Notch", "jeb_"]);
  });

  it("hides a player listed under the registry UUID", async () => {
    stubLists([{ uuid: NOTCH_REGISTRY, name: "Notch" }]);
    expect(await offered()).toEqual(["jeb_"]);
  });

  it("does not offer a player already on the list under the server's own UUID", async () => {
    stubLists([{ uuid: NOTCH_OFFLINE, name: "Notch" }]);
    expect(await offered()).toEqual(["jeb_"]);
  });

  it("matches the listed name case-insensitively", async () => {
    stubLists([{ uuid: NOTCH_OFFLINE, name: "notch" }]);
    expect(await offered()).toEqual(["jeb_"]);
  });
});

describe("PlayersTab add picker, stale names", () => {
  // Online-mode: player ROB (uuid ROB) was whitelisted as "Bob", then renamed
  // to "Rob"; the file keeps the old name. Another registry player now owns
  // the name "Bob" under a different UUID. The list entry is already accounted
  // for by Rob's UUID, so its stale name must not hide the new Bob.
  const ROB = "11111111111111111111111111111111";
  const NEW_BOB = "22222222222222222222222222222222";

  it("offers a registry player whose name only matches a list entry already claimed by another registry UUID", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url === "/api/servers/srv/playerlists")
          return json({ whitelist: [{ uuid: ROB, name: "Bob" }], ops: [], banned: [] });
        if (url === "/api/players")
          return json([
            { uuid: ROB, name: "Rob", added_at: "2026-01-01T00:00:00" },
            { uuid: NEW_BOB, name: "Bob", added_at: "2026-01-01T00:00:00" },
          ]);
        if (url === "/api/servers/srv/players/online") return json([]);
        throw new Error(`unexpected fetch ${url}`);
      }),
    );
    render(
      <ToastProvider>
        <PlayersTab serverId="srv" msmp={{ connected: true, schema_version: "3.1.0" }} />
      </ToastProvider>,
    );
    // Wait for both fetches to land: the list row shows and the Operators
    // picker (empty list) offers both registry players.
    await screen.findByRole("listitem");
    await waitFor(() =>
      expect(
        screen.getByLabelText("Player to add to Operators").querySelectorAll("option").length,
      ).toBe(3),
    );
    const picker = screen.getByLabelText("Player to add to Whitelist");
    await waitFor(() =>
      expect(
        Array.from(picker.querySelectorAll("option"))
          .slice(1)
          .map((o) => o.textContent),
      ).toEqual(["Bob"]),
    );
  });
});

// QA: the ban reason field and its failure path.
describe("PlayersTab ban reason (QA)", () => {
  function stubWithPost(postStatus: number, posts: unknown[]) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (url === "/api/servers/srv/playerlists/banned" && init?.method === "POST") {
          posts.push(JSON.parse(String(init.body)));
          if (postStatus !== 200) {
            return new Response(JSON.stringify({ detail: "reason is longer than 200 characters" }), {
              status: postStatus,
              headers: { "Content-Type": "application/json" },
            });
          }
          return json({ whitelist: [], ops: [], banned: [{ uuid: JEB, name: "jeb_" }] });
        }
        if (url === "/api/servers/srv/playerlists") return json({ whitelist: [], ops: [], banned: [] });
        if (url === "/api/players") return json(registry);
        if (url === "/api/servers/srv/players/online") return json([]);
        throw new Error(`unexpected fetch ${url}`);
      }),
    );
  }

  async function renderTab() {
    render(
      <ToastProvider>
        <PlayersTab serverId="srv" msmp={{ connected: true, schema_version: "3.1.0" }} />
      </ToastProvider>,
    );
    return waitFor(() => {
      const el = screen.getByLabelText("Player to add to Banned") as HTMLSelectElement;
      expect(el.querySelectorAll("option").length).toBeGreaterThan(1);
      return el;
    });
  }

  it("caps the reason at the backend's 200 characters", async () => {
    stubWithPost(200, []);
    await renderTab();
    const reason = screen.getByLabelText("Ban reason") as HTMLInputElement;
    expect(reason.maxLength).toBe(200);
  });

  it("keeps the pick and the reason for a retry when the add fails", async () => {
    const posts: unknown[] = [];
    stubWithPost(422, posts);
    const picker = await renderTab();
    fireEvent.change(picker, { target: { value: JEB } });
    const reason = screen.getByLabelText("Ban reason") as HTMLInputElement;
    fireEvent.change(reason, { target: { value: "griefing" } });
    await act(async () => {
      fireEvent.click(
        within(screen.getByRole("region", { name: "Banned" })).getByRole("button", {
          name: "Add",
        }),
      );
    });
    await waitFor(() => expect(posts).toEqual([{ uuid: JEB, reason: "griefing" }]));
    expect(reason.value).toBe("griefing");
    expect(picker.value).toBe(JEB);
  });

  it("does not promise a fixed op level 4 for operators", async () => {
    stubWithPost(200, []);
    await renderTab();
    // Operators get the server's op-permission-level, not always 4.
    expect(within(screen.getByRole("region", { name: "Operators" })).queryByText(/level 4/)).toBeNull();
  });
});
