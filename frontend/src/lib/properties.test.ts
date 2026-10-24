import { describe, expect, it } from "vitest";
import type { PropertiesResponse, PropertyDefinition } from "../api/servers";
import { applyHint, pendingEdits, propertyRows, saveOutcome, savedMessage } from "./properties";

function def(managed = false): PropertyDefinition {
  return {
    type: "string",
    values: null,
    min: null,
    max: null,
    description: "",
    default: null,
    managed,
    live: false,
  };
}

function response(
  properties: Record<string, string>,
  definitions: Record<string, PropertyDefinition>,
): PropertiesResponse {
  return { properties, definitions, applied_live: [] };
}

describe("propertyRows", () => {
  it("lists known keys first, then file-only keys, each alphabetical", () => {
    const data = response(
      { "zeta-mod": "1", motd: "hi", "alpha-mod": "2" },
      { motd: def(), difficulty: def() },
    );
    expect(propertyRows(data)).toEqual(["difficulty", "motd", "alpha-mod", "zeta-mod"]);
  });

  it("hides managed keys absent from the file", () => {
    const data = response(
      { motd: "hi" },
      { motd: def(), "management-server-enabled": def(true) },
    );
    expect(propertyRows(data)).toEqual(["motd"]);
  });

  it("shows managed keys once Lectern has written them", () => {
    const data = response(
      { "management-server-port": "25585", "management-server-enabled": "true" },
      {
        "management-server-enabled": def(true),
        "management-server-port": def(true),
        "management-server-secret": def(true),
      },
    );
    expect(propertyRows(data)).toEqual(["management-server-enabled", "management-server-port"]);
  });
});

describe("saveOutcome", () => {
  it("marks keys in applied_live as live and every other saved key as file", () => {
    expect(saveOutcome(["motd", "pvp", "difficulty"], ["motd", "difficulty"])).toEqual({
      motd: "live",
      pvp: "file",
      difficulty: "live",
    });
  });

  it("maps everything to file when nothing applied live", () => {
    expect(saveOutcome(["motd", "pvp"], [])).toEqual({ motd: "file", pvp: "file" });
  });

  it("leaves out keys that were not saved, even if reported live", () => {
    const outcome = saveOutcome(["pvp"], ["motd"]);
    expect(outcome).toEqual({ pvp: "file" });
    expect("motd" in outcome).toBe(false);
  });

  it("is empty for an empty save", () => {
    expect(saveOutcome([], [])).toEqual({});
  });
});

describe("applyHint", () => {
  const base = { edited: false, live: false, connected: false, lastSave: null } as const;

  it("says applies now while editing a live key with a connected client", () => {
    expect(applyHint({ ...base, edited: true, live: true, connected: true })).toBe("applies now");
  });

  it("says next start while editing a live key without a connected client", () => {
    expect(applyHint({ ...base, edited: true, live: true, connected: false })).toBe(
      "applies at next start",
    );
  });

  it("says next start while editing a non-live key, connected or not", () => {
    expect(applyHint({ ...base, edited: true, connected: true })).toBe("applies at next start");
    expect(applyHint({ ...base, edited: true })).toBe("applies at next start");
  });

  it("reports the last save when the row is not being edited", () => {
    expect(applyHint({ ...base, live: true, connected: true, lastSave: "live" })).toBe(
      "applied now",
    );
    expect(applyHint({ ...base, live: true, connected: true, lastSave: "file" })).toBe(
      "applies at next start",
    );
  });

  it("lets a new edit replace the post-save hint", () => {
    // Saved live, then edited again while disconnected: the edit wins.
    expect(applyHint({ ...base, edited: true, live: true, lastSave: "live" })).toBe(
      "applies at next start",
    );
    // Saved to the file, then edited again while connected: the edit wins.
    expect(
      applyHint({ ...base, edited: true, live: true, connected: true, lastSave: "file" }),
    ).toBe("applies now");
  });

  it("shows nothing for an untouched row", () => {
    expect(applyHint(base)).toBeNull();
  });

  it("drops the new-players narrowing for gamemode when force-gamemode is on", () => {
    const gm = { ...base, edited: true, live: true, connected: true, key: "gamemode" };
    expect(applyHint(gm)).toBe("applies now to new players");
    expect(applyHint({ ...gm, forceGamemode: true })).toBe("applies now");
    expect(applyHint({ ...base, live: true, lastSave: "live", key: "gamemode", forceGamemode: true })).toBe(
      "applied now",
    );
    expect(applyHint({ ...base, live: true, connected: true })).toBeNull();
  });

  it("narrows gamemode's live wording to new players", () => {
    const gm = { ...base, key: "gamemode", live: true, connected: true };
    expect(applyHint({ ...gm, edited: true })).toBe("applies now to new players");
    expect(applyHint({ ...gm, lastSave: "live" })).toBe("applied now to new players");
    expect(applyHint({ ...gm, lastSave: "file" })).toBe("applies at next start");
    expect(applyHint({ ...gm, edited: true, connected: false })).toBe("applies at next start");
    expect(applyHint({ ...base, key: "motd", edited: true, live: true, connected: true })).toBe(
      "applies now",
    );
  });
});

describe("savedMessage", () => {
  it("is the plain message when nothing applied live", () => {
    expect(savedMessage([])).toBe("server.properties saved.");
  });

  it("counts the settings applied now, singular and plural", () => {
    expect(savedMessage(["motd"])).toBe("server.properties saved — 1 setting applied now.");
    expect(savedMessage(["motd", "difficulty"])).toBe(
      "server.properties saved — 2 settings applied now.",
    );
  });
});

describe("pendingEdits", () => {
  it("drops edits the file already holds and keeps the rest", () => {
    expect(
      pendingEdits({ motd: "x", "level-seed": "42" }, { motd: "hi", "level-seed": "42" }),
    ).toEqual({ motd: "x" });
  });

  it("treats a key absent from the file as empty", () => {
    expect(pendingEdits({ "mod-key": "", other: "1" }, {})).toEqual({ other: "1" });
  });

  it("returns nothing when every edit is on disk", () => {
    expect(pendingEdits({ pvp: "false" }, { pvp: "false" })).toEqual({});
  });
});
