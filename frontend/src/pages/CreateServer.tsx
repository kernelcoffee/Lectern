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
  ServerType,
  suggestServerDefaults,
} from "../api/servers";

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

  const [submitting, setSubmitting] = useState(false);
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
    applySuggestion();
    if (type) {
      chooseType(type); // re-preselects the latest version (+ newest loader)
    } else {
      setMcVersion("");
      setLoaders([]);
      setLoader("");
    }
  }

  const ready =
    type !== null &&
    mcVersion !== "" &&
    name.trim() !== "" &&
    (!type.needs_loader || loader !== "");

  async function submit() {
    if (!type) return;
    setSubmitting(true);
    setError(null);
    try {
      await createServer({
        name: name.trim(),
        type: type.key as ServerType,
        mc_version: mcVersion,
        loader_version: type.needs_loader ? loader : null,
        port,
        memory_mb: memory,
      });
      onCreated();
    } catch (e) {
      fail(e);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="p-6 space-y-5">
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
                min={1}
                max={65535}
                value={port}
                onChange={(e) => setPort(Number(e.target.value))}
                className="w-full bg-slate-800 border border-slate-700 rounded px-2.5 py-1.5"
              />
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
        </div>

        <div className="flex items-center gap-2 pt-1">
          <button
            type="submit"
            disabled={!ready || submitting}
            className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 rounded px-4 py-1.5 text-sm font-medium text-slate-900"
          >
            {submitting ? "Creating…" : "Build server"}
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
      </form>

      {error && <p className="text-sm text-red-400">{error}</p>}
    </div>
  );
}
