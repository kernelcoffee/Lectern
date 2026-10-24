// @vitest-environment jsdom
// The per-row "applies now / applies at next start" hints must match what the
// backend will actually do (live only while the server is `running` with a
// connected client) and must not outlive a stop or a restart.
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PropertyDefinition, ServerDetail, ServerStatus } from "../../api/servers";
import { ToastProvider } from "../../components/Toasts";
import PropertiesTab from "./PropertiesTab";

const def = (live: boolean): PropertyDefinition => ({
  type: "string",
  values: null,
  min: null,
  max: null,
  description: "",
  default: null,
  managed: false,
  live,
});

const definitions = { motd: def(true), "level-seed": def(false) };
let fileProps: Record<string, string> = { motd: "hi", "level-seed": "1" };
// Keys the stubbed PATCH reports as applied through the protocol.
let liveOnPatch: string[] = [];

function server(status: ServerStatus, connected: boolean | null): ServerDetail {
  return {
    id: "srv",
    name: "srv",
    type: "fabric",
    mc_version: "26.3",
    loader_version: "0.17.0",
    port: 25565,
    memory_mb: 2048,
    status,
    created_at: "2026-01-01T00:00:00Z",
    jvm_args: "",
    auto_start: false,
    auto_start_delay: 0,
    crash_restart: false,
    stop_command: "stop",
    shutdown_timeout: 60,
    log_retention_days: 0,
    backup_excluded: "",
    backup_max: 5,
    backup_compress: true,
    backup_stop_server: false,
    eula_accepted: true,
    running: status === "running",
    msmp: connected === null ? null : { connected, schema_version: "3.1.0" },
  };
}

function json(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  fileProps = { motd: "hi", "level-seed": "1" };
  liveOnPatch = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/servers/srv/properties" && (!init || !init.method)) {
        return json({ properties: fileProps, definitions, applied_live: [] });
      }
      if (url === "/api/servers/srv/properties" && init?.method === "PATCH") {
        const body = JSON.parse(String(init.body)) as Record<string, string>;
        fileProps = { ...fileProps, ...body };
        return json({
          properties: fileProps,
          definitions,
          applied_live: Object.keys(body).filter((k) => liveOnPatch.includes(k)),
        });
      }
      throw new Error(`unexpected fetch ${init?.method ?? "GET"} ${url}`);
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** The <label> of the server.properties row named `key`. */
function rowLabel(key: string): HTMLLabelElement {
  const label = Array.from(document.querySelectorAll("label")).find((l) =>
    (l.querySelector("span")?.textContent ?? "").startsWith(key),
  );
  if (!label) throw new Error(`no row ${key}`);
  return label;
}

function ui(s: ServerDetail) {
  return (
    <ToastProvider>
      <PropertiesTab serverId="srv" server={s} onServerUpdate={() => {}} />
    </ToastProvider>
  );
}

/** Edits `key`'s text input. */
function edit(key: string, value: string) {
  fireEvent.change(rowLabel(key).querySelector("input")!, { target: { value } });
}

/** Clicks the server.properties Save (the second Save on the tab). */
async function saveProperties() {
  await act(async () => {
    fireEvent.click(screen.getAllByRole("button", { name: "Save" })[1]);
  });
}

describe("PropertiesTab apply hints", () => {
  it("promises applies now on a running server with a connected client", async () => {
    render(ui(server("running", true)));
    await waitFor(() => rowLabel("motd"));
    expect(screen.getByText(/management protocol connected, motd/)).toBeTruthy();
    edit("motd", "x");
    expect(rowLabel("motd").textContent).toContain("— applies now");
    edit("level-seed", "42");
    expect(rowLabel("level-seed").textContent).toContain("— applies at next start");
  });

  it("does not promise applies now while the server is stopping with the client still connected", async () => {
    // The backend applies live keys only when status == running
    // (_apply_live_settings); during `stopping`/`starting` the process and
    // its connected client still exist, so server.msmp.connected is true.
    render(ui(server("stopping", true)));
    await waitFor(() => rowLabel("motd"));
    edit("motd", "x");
    expect(rowLabel("motd").textContent).toContain("— applies at next start");
    expect(screen.queryByText(/management protocol connected, motd/)).toBeNull();
  });

  it("does not promise applies now while the server is starting with the client connected", async () => {
    render(ui(server("starting", true)));
    await waitFor(() => rowLabel("motd"));
    edit("motd", "x");
    expect(rowLabel("motd").textContent).toContain("— applies at next start");
    expect(screen.queryByText(/management protocol connected, motd/)).toBeNull();
  });

  it("drops the post-save 'applies at next start' hint once the server has restarted", async () => {
    const { rerender } = render(ui(server("running", true)));
    await waitFor(() => rowLabel("level-seed"));
    edit("level-seed", "42");
    await saveProperties();
    await waitFor(() =>
      expect(rowLabel("level-seed").textContent).toContain("— applies at next start"),
    );
    // Stop, then start again from the header: the tab stays mounted.
    rerender(ui(server("stopped", null)));
    rerender(ui(server("starting", false)));
    rerender(ui(server("running", true)));
    // The saved value is now in effect; claiming it still waits for the next
    // start is false.
    expect(rowLabel("level-seed").textContent).not.toContain("applies at next start");
  });

  it("drops an 'applied now' hint once the server stops", async () => {
    liveOnPatch = ["motd"];
    const { rerender } = render(ui(server("running", true)));
    await waitFor(() => rowLabel("motd"));
    edit("motd", "x");
    await saveProperties();
    await waitFor(() => expect(rowLabel("motd").textContent).toContain("— applied now"));
    rerender(ui(server("stopping", true)));
    expect(rowLabel("motd").textContent).not.toContain("applied now");
  });

  it("keeps the post-save hint while the status is unchanged", async () => {
    liveOnPatch = ["motd"];
    const { rerender } = render(ui(server("running", true)));
    await waitFor(() => rowLabel("motd"));
    edit("motd", "x");
    await saveProperties();
    await waitFor(() => expect(rowLabel("motd").textContent).toContain("— applied now"));
    // A fresh detail object with the same status (the page re-polls).
    rerender(ui(server("running", true)));
    expect(rowLabel("motd").textContent).toContain("— applied now");
  });

  it("keeps unsaved edits across a status change", async () => {
    const { rerender } = render(ui(server("running", true)));
    await waitFor(() => rowLabel("level-seed"));
    edit("level-seed", "42");
    rerender(ui(server("stopping", true)));
    expect(rowLabel("level-seed").querySelector("input")!.value).toBe("42");
    expect(rowLabel("level-seed").textContent).toContain("— applies at next start");
  });
});

// QA: force-gamemode is not a live key, so only the value the running server
// started with decides whether a live gamemode change reaches everyone online.
describe("PropertiesTab gamemode hint vs force-gamemode (QA)", () => {
  it("keeps 'to new players' while force-gamemode=true is only a pending edit", async () => {
    const gmDefs = {
      ...definitions,
      gamemode: def(true),
      "force-gamemode": def(false),
    };
    fileProps = { motd: "hi", gamemode: "survival", "force-gamemode": "false" };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url === "/api/servers/srv/properties") {
          return json({ properties: fileProps, definitions: gmDefs, applied_live: [] });
        }
        throw new Error(`unexpected fetch ${url}`);
      }),
    );
    render(ui(server("running", true)));
    await waitFor(() => rowLabel("gamemode"));
    edit("force-gamemode", "true");
    edit("gamemode", "creative");
    // The running server still has force-gamemode=false: the live setter only
    // changes the default for players who have never joined.
    expect(rowLabel("gamemode").textContent).toContain("— applies now to new players");
  });
});

// QA: a 504 from PATCH means the file (and the DB port) were already written
// with every key except the unconfirmed live one; the form must not keep
// presenting those saved values as unsaved edits.
describe("PropertiesTab after a 504 (QA)", () => {
  it("reloads the file and stops marking saved keys as pending", async () => {
    let afterPatch = false;
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/servers/srv/properties" && init?.method === "PATCH") {
        afterPatch = true;
        fileProps = { ...fileProps, "level-seed": "42" };
        return new Response(
          JSON.stringify({
            detail:
              "The server did not confirm the motd change in time and may still apply it. level-seed saved to server.properties for the next start.",
          }),
          { status: 504, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url === "/api/servers/srv/properties") {
        return json({ properties: fileProps, definitions, applied_live: [] });
      }
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(ui(server("running", true)));
    await waitFor(() => rowLabel("level-seed"));
    edit("motd", "x");
    edit("level-seed", "42");
    await saveProperties();
    await waitFor(() => expect(screen.getByText(/did not confirm the motd change/)).toBeTruthy());
    expect(afterPatch).toBe(true);
    // level-seed is on disk now: it is no longer an unsaved edit.
    await waitFor(() =>
      expect(rowLabel("level-seed").textContent).not.toContain("— applies at next start"),
    );
  });
});

describe("PropertiesTab gamemode hint vs a saved force-gamemode", () => {
  const gmDefs = { ...definitions, gamemode: def(true), "force-gamemode": def(false) };

  function stub(onPatch: (body: Record<string, string>) => void = () => {}) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (url !== "/api/servers/srv/properties") throw new Error(`unexpected fetch ${url}`);
        if (init?.method === "PATCH") {
          const body = JSON.parse(String(init.body)) as Record<string, string>;
          onPatch(body);
          fileProps = { ...fileProps, ...body };
        }
        return json({ properties: fileProps, definitions: gmDefs, applied_live: [] });
      }),
    );
  }

  it("says applies now when the server started with force-gamemode=true", async () => {
    fileProps = { gamemode: "survival", "force-gamemode": "true" };
    stub();
    render(ui(server("running", true)));
    await waitFor(() => rowLabel("gamemode"));
    edit("gamemode", "creative");
    expect(rowLabel("gamemode").textContent).toMatch(/— applies now$/);
  });

  it("keeps 'to new players' after force-gamemode=true was saved while running", async () => {
    fileProps = { gamemode: "survival", "force-gamemode": "false" };
    stub();
    render(ui(server("running", true)));
    await waitFor(() => rowLabel("gamemode"));
    edit("force-gamemode", "true");
    await saveProperties();
    await waitFor(() =>
      expect(rowLabel("force-gamemode").textContent).toContain("— applies at next start"),
    );
    // The file now says true, but the running server still has false.
    edit("gamemode", "creative");
    expect(rowLabel("gamemode").textContent).toContain("— applies now to new players");
  });

  it("trusts the file again once the server has restarted", async () => {
    fileProps = { gamemode: "survival", "force-gamemode": "false" };
    stub();
    const { rerender } = render(ui(server("running", true)));
    await waitFor(() => rowLabel("gamemode"));
    edit("force-gamemode", "true");
    await saveProperties();
    await waitFor(() =>
      expect(rowLabel("force-gamemode").textContent).toContain("— applies at next start"),
    );
    rerender(ui(server("stopped", null)));
    rerender(ui(server("running", true)));
    edit("gamemode", "creative");
    expect(rowLabel("gamemode").textContent).toMatch(/— applies now$/);
  });
});

describe("PropertiesTab after a failed save", () => {
  function stubFailure(
    status: number,
    writes: Record<string, string>,
    reread: "ok" | "fail" = "ok",
  ): { gets: () => number } {
    let gets = 0;
    let patched = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (url !== "/api/servers/srv/properties") throw new Error(`unexpected fetch ${url}`);
        if (init?.method === "PATCH") {
          patched = true;
          fileProps = { ...fileProps, ...writes };
          return new Response(JSON.stringify({ detail: `failed with ${status}` }), {
            status,
            headers: { "Content-Type": "application/json" },
          });
        }
        gets += 1;
        if (patched && reread === "fail") {
          return new Response("down", { status: 502 });
        }
        return json({ properties: fileProps, definitions, applied_live: [] });
      }),
    );
    return { gets: () => gets };
  }

  it("keeps the unconfirmed live edit pending after a 504", async () => {
    stubFailure(504, { "level-seed": "42" });
    render(ui(server("running", true)));
    await waitFor(() => rowLabel("motd"));
    edit("motd", "x");
    edit("level-seed", "42");
    await saveProperties();
    await waitFor(() => expect(rowLabel("level-seed").textContent).toBe("level-seed"));
    // motd is not in the file: still an edit, still offered for a retry.
    expect(rowLabel("motd").querySelector("input")!.value).toBe("x");
    expect(rowLabel("motd").textContent).toContain("— applies now");
    expect(screen.getByText("failed with 504")).toBeTruthy();
  });

  it("does not re-read the file after a 422, which wrote nothing", async () => {
    const counter = stubFailure(422, {});
    render(ui(server("running", true)));
    await waitFor(() => rowLabel("level-seed"));
    edit("level-seed", "7");
    await saveProperties();
    await waitFor(() => expect(screen.getByText("failed with 422")).toBeTruthy());
    // Only the initial load: a 422 is not reconciled against the file.
    expect(counter.gets()).toBe(1);
    expect(rowLabel("level-seed").querySelector("input")!.value).toBe("7");
  });

  it("keeps every edit when the re-read fails too", async () => {
    stubFailure(504, { "level-seed": "42" }, "fail");
    render(ui(server("running", true)));
    await waitFor(() => rowLabel("level-seed"));
    edit("level-seed", "42");
    await saveProperties();
    await waitFor(() => expect(screen.getByText("failed with 504")).toBeTruthy());
    expect(rowLabel("level-seed").textContent).toContain("— applies at next start");
  });

  it("clears the add-property row when the failed save still wrote it", async () => {
    stubFailure(504, { "mod-key": "on" });
    render(ui(server("running", true)));
    await waitFor(() => rowLabel("motd"));
    fireEvent.change(screen.getByPlaceholderText("key"), { target: { value: "mod-key" } });
    fireEvent.change(screen.getByPlaceholderText("value"), { target: { value: "on" } });
    await saveProperties();
    await waitFor(() => expect(rowLabel("mod-key").querySelector("input")!.value).toBe("on"));
    expect((screen.getByPlaceholderText("key") as HTMLInputElement).value).toBe("");
    expect((screen.getByPlaceholderText("value") as HTMLInputElement).value).toBe("");
  });

  it("keeps the add-property row when the failed save did not write it", async () => {
    stubFailure(504, {});
    render(ui(server("running", true)));
    await waitFor(() => rowLabel("motd"));
    fireEvent.change(screen.getByPlaceholderText("key"), { target: { value: "mod-key" } });
    fireEvent.change(screen.getByPlaceholderText("value"), { target: { value: "on" } });
    await saveProperties();
    await waitFor(() => expect(screen.getByText("failed with 504")).toBeTruthy());
    expect((screen.getByPlaceholderText("key") as HTMLInputElement).value).toBe("mod-key");
  });
});
