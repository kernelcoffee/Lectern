// New-server page, Crafty-wizard-inspired: one card with the identity fields
// (name, server type, Minecraft version, loader build) and a "Quick settings"
// section (port, memory), closed by Build/Reset buttons.
//
// Everything is preselected to the most common choice: first server type,
// latest stable Minecraft version (both catalogs are newest-first), newest
// loader build — so a fresh page is submittable as-is. Picking a type reloads
// its version list (re-preselecting the latest), picking a version reloads
// loader builds. Submit posts the record and the backend installs in the
// background — the caller navigates to the dashboard where the row shows
// live install progress.

import { useEffect, useState } from "react";
import { ApiError } from "../api/client";
import {
  getFabricLoaders,
  getMinecraftVersions,
  getServerTypes,
  ServerTypeInfo,
} from "../api/catalog";
import {
  createServer,
  getServerProgress,
  importWorld,
  ServerType,
  suggestServerDefaults,
} from "../api/servers";

type WorldMode = "none" | "upload" | "url";

const TYPE_LABELS: Record<string, string> = {
  vanilla: "Vanilla",
  fabric: "Fabric",
};

export default function CreateServer({ onCreated }: { onCreated: () => void }) {
  const [types, setTypes] = useState<ServerTypeInfo[]>([]);
  const [type, setType] = useState<ServerTypeInfo | null>(null);

  const [mcVersions, setMcVersions] = useState<string[]>([]);
  const [mcVersion, setMcVersion] = useState("");
  const [loadingVersions, setLoadingVersions] = useState(false);

  const [loaders, setLoaders] = useState<string[]>([]);
  const [loader, setLoader] = useState("");
  const [loadingLoaders, setLoadingLoaders] = useState(false);

  const [name, setName] = useState("");
  const [port, setPort] = useState(25565);
  const [memory, setMemory] = useState(2048);
  const [seed, setSeed] = useState("");
  const [whitelist, setWhitelist] = useState(true); // secure by default

  const [worldMode, setWorldMode] = useState<WorldMode>("none");
  const [worldFile, setWorldFile] = useState<File | null>(null);
  const [worldUrl, setWorldUrl] = useState("");
  // Skip bloat like Distant Horizons' multi-GB LOD cache by default.
  const [worldExclude, setWorldExclude] = useState("*DistantHorizons*");
  const [uploadPct, setUploadPct] = useState<number | null>(null);

  const [submitting, setSubmitting] = useState(false);
  // Progress line while a world import runs (install can take minutes).
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fail = (e: unknown) =>
    setError(e instanceof ApiError ? e.message : String(e));

  // Prefill name/port with the backend's first-free suggestion ("New server
  // 2", port 25566, …) so stacking servers needs no manual deconfliction.
  // Applied only to untouched fields — a slow response must never overwrite
  // something the user already typed.
  const applySuggestion = () =>
    suggestServerDefaults()
      .then((s) => {
        setName((prev) => (prev === "" ? s.name : prev));
        setPort((prev) => (prev === 25565 ? s.port : prev));
        setMemory((prev) => (prev === 2048 ? s.memory_mb : prev));
      })
      .catch(() => {
        // Suggestions are a convenience — the form still works without them.
      });

  // Load types once and preselect the first (vanilla).
  useEffect(() => {
    applySuggestion();
    getServerTypes()
      .then((ts) => {
        setTypes(ts);
        if (ts.length > 0) chooseType(ts[0]);
      })
      .catch(fail);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function chooseType(t: ServerTypeInfo) {
    setType(t);
    setMcVersion("");
    setLoaders([]);
    setLoader("");
    setLoadingVersions(true);
    try {
      const versions = await getMinecraftVersions(t.key);
      setMcVersions(versions);
      // Preselect the latest stable (catalogs are newest-first).
      if (versions.length > 0) await chooseVersion(versions[0], t);
    } catch (e) {
      fail(e);
    } finally {
      setLoadingVersions(false);
    }
  }

  // `forType` avoids reading stale state when called from chooseType.
  async function chooseVersion(v: string, forType: ServerTypeInfo | null = type) {
    setMcVersion(v);
    if (!forType?.needs_loader || !v) {
      setLoaders([]);
      setLoader("");
      return;
    }
    // Clear the previous version's build immediately — it must not be
    // submittable against the new version while the list loads.
    setLoader("");
    setLoadingLoaders(true);
    try {
      const ls = await getFabricLoaders(v);
      setLoaders(ls);
      setLoader(ls[0] ?? "");
    } catch (e) {
      fail(e);
    } finally {
      setLoadingLoaders(false);
    }
  }

  function reset() {
    setName("");
    setPort(25565);
    setMemory(2048);
    setSeed("");
    setWhitelist(true);
    setWorldMode("none");
    setWorldFile(null);
    setWorldUrl("");
    setWorldExclude("*DistantHorizons*");
    setUploadPct(null);
    applySuggestion();
    if (type) {
      chooseType(type); // re-preselects the latest version (+ newest loader)
    } else {
      setMcVersion("");
      setLoaders([]);
      setLoader("");
    }
  }

  const worldReady =
    worldMode === "none" ||
    (worldMode === "upload" ? worldFile !== null : worldUrl.trim() !== "");

  const ready =
    type !== null &&
    mcVersion !== "" &&
    name.trim() !== "" &&
    (!type.needs_loader || loader !== "") &&
    worldReady;

  // Poll install progress to completion — the world can only be imported once
  // the pipeline has provisioned the server dir (path set, status stopped).
  async function waitForInstalled(id: string) {
    for (let i = 0; i < 900; i++) {
      const p = await getServerProgress(id);
      if (p.error) throw new ApiError(0, `Install failed: ${p.error}`);
      setStatusMsg(p.message || "Provisioning server…");
      if (p.done) return;
      await new Promise((r) => setTimeout(r, 1000));
    }
    throw new ApiError(0, "Timed out waiting for the server to install");
  }

  async function submit() {
    if (!type) return;
    setSubmitting(true);
    setError(null);
    try {
      const server = await createServer({
        name: name.trim(),
        type: type.key as ServerType,
        mc_version: mcVersion,
        loader_version: type.needs_loader ? loader : null,
        port,
        memory_mb: memory,
        seed: worldMode === "none" ? seed.trim() : "",
        whitelist,
      });
      if (worldMode !== "none") {
        await waitForInstalled(server.id);
        if (worldMode === "upload") {
          setStatusMsg("Uploading world…");
          setUploadPct(0);
          await importWorld(
            server.id,
            { file: worldFile!, exclude: worldExclude },
            (f) => {
              setUploadPct(f);
              if (f >= 1) setStatusMsg("Extracting world…");
            },
          );
        } else {
          setStatusMsg("Importing world…");
          await importWorld(server.id, {
            url: worldUrl.trim(),
            exclude: worldExclude,
          });
        }
      }
      onCreated();
    } catch (e) {
      fail(e);
    } finally {
      setSubmitting(false);
      setStatusMsg(null);
      setUploadPct(null);
    }
  }

  return (
    <div className="p-4 sm:p-6 space-y-5">
      <h2 className="text-xl font-semibold">New server</h2>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        className="rounded-lg border border-slate-800 p-5 space-y-5 max-w-3xl"
      >
        <div className="grid sm:grid-cols-2 gap-4">
          <label className="text-sm space-y-1 sm:col-span-2">
            <span className="text-slate-400">Server name</span>
            <input
              required
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="My new server"
              className="w-full bg-slate-800 border border-slate-700 rounded px-2.5 py-1.5"
            />
          </label>

          <label className="text-sm space-y-1">
            <span className="text-slate-400">Server type</span>
            <select
              value={type?.key ?? ""}
              onChange={(e) => {
                const t = types.find((x) => x.key === e.target.value);
                if (t) chooseType(t);
              }}
              disabled={types.length === 0}
              className="w-full bg-slate-800 border border-slate-700 rounded px-2 py-1.5 disabled:opacity-50"
            >
              {types.length === 0 && <option value="">Loading types…</option>}
              {types.map((t) => (
                <option key={t.key} value={t.key}>
                  {TYPE_LABELS[t.key] ?? t.key}
                  {t.needs_loader ? " (mod loader)" : ""}
                </option>
              ))}
            </select>
          </label>

          <label className="text-sm space-y-1">
            <span className="text-slate-400">Minecraft version</span>
            {loadingVersions ? (
              <p className="text-sm text-slate-500 py-1.5">Loading versions…</p>
            ) : (
              <select
                required
                value={mcVersion}
                onChange={(e) => chooseVersion(e.target.value)}
                className="w-full bg-slate-800 border border-slate-700 rounded px-2 py-1.5"
              >
                <option value="" disabled>
                  Select a version…
                </option>
                {mcVersions.map((v) => (
                  <option key={v} value={v}>
                    {v}
                  </option>
                ))}
              </select>
            )}
          </label>

          {type?.needs_loader && (
            <label className="text-sm space-y-1">
              <span className="text-slate-400">Fabric loader build</span>
              {loadingLoaders ? (
                <p className="text-sm text-slate-500 py-1.5">Loading loaders…</p>
              ) : (
                <select
                  value={loader}
                  onChange={(e) => setLoader(e.target.value)}
                  disabled={loaders.length === 0}
                  className="w-full bg-slate-800 border border-slate-700 rounded px-2 py-1.5 disabled:opacity-50"
                >
                  {loaders.length === 0 && (
                    <option value="">Pick a version first</option>
                  )}
                  {loaders.map((l) => (
                    <option key={l} value={l}>
                      {l}
                    </option>
                  ))}
                </select>
              )}
            </label>
          )}
        </div>

        {/* Quick settings (Crafty's section of the same name) */}
        <div className="space-y-3">
          <div>
            <h3 className="text-sm font-medium text-slate-300">
              Quick settings{" "}
              <span className="text-xs font-normal text-slate-500">
                — everything can be changed later in Properties
              </span>
            </h3>
            <hr className="mt-2 border-slate-800" />
          </div>
          <div className="grid grid-cols-2 gap-4 max-w-md">
            <label className="text-sm space-y-1">
              <span className="text-slate-400">Server port</span>
              <input
                type="number"
                min={1024}
                max={65535}
                value={port}
                onChange={(e) => setPort(Number(e.target.value))}
                className="w-full bg-slate-800 border border-slate-700 rounded px-2.5 py-1.5"
              />
              <span className="block text-xs text-slate-500">
                Any port 1024–65535. Servers can share a port as long as only
                one runs at a time.
              </span>
            </label>
            <label className="text-sm space-y-1">
              <span className="text-slate-400">Memory (MB)</span>
              <input
                type="number"
                min={256}
                step={256}
                value={memory}
                onChange={(e) => setMemory(Number(e.target.value))}
                className="w-full bg-slate-800 border border-slate-700 rounded px-2.5 py-1.5"
              />
            </label>
          </div>

          {/* World seed — first-generation only; a matching world ignores it. */}
          <label className="block text-sm space-y-1 max-w-md">
            <span className="text-slate-400">World seed (optional)</span>
            <input
              type="text"
              value={seed}
              disabled={worldMode !== "none"}
              onChange={(e) => setSeed(e.target.value)}
              placeholder="Leave empty for a random world"
              className="w-full bg-slate-800 border border-slate-700 rounded px-2.5 py-1.5 disabled:opacity-50"
            />
            <span className="block text-xs text-slate-500">
              {worldMode === "none"
                ? "Numbers or text — text seeds are hashed, just like in-game."
                : "Ignored when importing a world — that world's seed is kept."}
            </span>
          </label>
        </div>

        {/* Security */}
        <div className="space-y-3">
          <div>
            <h3 className="text-sm font-medium text-slate-300">Security</h3>
            <hr className="mt-2 border-slate-800" />
          </div>
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              checked={whitelist}
              onChange={(e) => setWhitelist(e.target.checked)}
              className="mt-0.5"
            />
            <span>
              <span className="text-slate-200">Enable whitelist</span>{" "}
              <span className="text-xs text-slate-500">(recommended)</span>
              <span className="mt-0.5 block text-xs text-slate-500">
                Only whitelisted players can join. After the server is created,
                add players to its whitelist from the{" "}
                <span className="text-slate-400">Players</span> tab — until then{" "}
                <span className="text-amber-400/90">nobody can connect</span>,
                including you.
              </span>
            </span>
          </label>
        </div>

        {/* Import a world (optional) */}
        <div className="space-y-3">
          <div>
            <h3 className="text-sm font-medium text-slate-300">
              Import a world{" "}
              <span className="text-xs font-normal text-slate-500">
                — optional; start on an existing map instead of a fresh one
              </span>
            </h3>
            <hr className="mt-2 border-slate-800" />
          </div>
          <div className="max-w-md space-y-3">
            <label className="text-sm space-y-1 block">
              <span className="text-slate-400">World source</span>
              <select
                value={worldMode}
                onChange={(e) => {
                  setWorldMode(e.target.value as WorldMode);
                  setError(null);
                }}
                className="w-full bg-slate-800 border border-slate-700 rounded px-2 py-1.5"
              >
                <option value="none">Don't import — generate a new world</option>
                <option value="upload">Upload a .zip</option>
                <option value="url">Download from a URL</option>
              </select>
            </label>

            {worldMode === "upload" && (
              <label className="text-sm space-y-1 block">
                <span className="text-slate-400">World .zip</span>
                <input
                  type="file"
                  accept=".zip,application/zip"
                  onChange={(e) => setWorldFile(e.target.files?.[0] ?? null)}
                  className="w-full text-sm text-slate-300 file:mr-3 file:rounded file:border-0 file:bg-slate-700 file:px-3 file:py-1.5 file:text-sm file:text-slate-100 hover:file:bg-slate-600"
                />
              </label>
            )}

            {worldMode === "url" && (
              <label className="text-sm space-y-1 block">
                <span className="text-slate-400">World .zip URL</span>
                <input
                  type="url"
                  value={worldUrl}
                  onChange={(e) => setWorldUrl(e.target.value)}
                  placeholder="https://example.com/my-world.zip"
                  className="w-full bg-slate-800 border border-slate-700 rounded px-2.5 py-1.5"
                />
              </label>
            )}

            {worldMode !== "none" && (
              <>
                <label className="text-sm space-y-1 block">
                  <span className="text-slate-400">
                    Skip files{" "}
                    <span className="text-xs text-slate-500">
                      — comma-separated patterns, e.g. mod caches
                    </span>
                  </span>
                  <input
                    value={worldExclude}
                    onChange={(e) => setWorldExclude(e.target.value)}
                    placeholder="*DistantHorizons*, *.tmp"
                    className="w-full bg-slate-800 border border-slate-700 rounded px-2.5 py-1.5 font-mono text-xs"
                  />
                </label>
                <p className="text-xs text-slate-500">
                  The archive's world folder (with <code>level.dat</code>) is
                  extracted into the new server; matching files are skipped.
                  Distant Horizons stores gigabytes of LOD cache in the world —
                  left in by default it's excluded here. Clear the field to
                  import everything. Best results when the world matches the
                  server's Minecraft version.
                </p>
              </>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2 pt-1">
          <button
            type="submit"
            disabled={!ready || submitting}
            className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 rounded px-4 py-1.5 text-sm font-medium text-slate-900"
          >
            {submitting
              ? statusMsg ?? "Creating…"
              : worldMode === "none"
                ? "Build server"
                : "Build server + import world"}
          </button>
          <button
            type="button"
            onClick={reset}
            className="bg-slate-800 hover:bg-slate-700 rounded px-4 py-1.5 text-sm"
          >
            Reset
          </button>
          <span className="text-xs text-slate-500 ml-2">
            The jar and a matching Java runtime are downloaded automatically.
          </span>
        </div>

        {/* Upload progress — a large world can take a while over the network. */}
        {uploadPct !== null && (
          <div className="space-y-1">
            <div className="flex justify-between text-xs text-slate-400">
              <span>{statusMsg ?? "Uploading world…"}</span>
              <span className="tabular-nums">{Math.round(uploadPct * 100)}%</span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded bg-slate-800">
              <div
                className="h-full rounded bg-emerald-500 transition-[width] duration-150"
                style={{ width: `${Math.max(2, uploadPct * 100)}%` }}
              />
            </div>
          </div>
        )}
      </form>

      {error && <p className="text-sm text-red-400">{error}</p>}
    </div>
  );
}
