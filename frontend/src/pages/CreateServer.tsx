import { useEffect, useState } from "react";
import { ApiError } from "../api/client";
import {
  getFabricLoaders,
  getMinecraftVersions,
  getServerTypes,
  ServerTypeInfo,
} from "../api/catalog";
import { createServer, ServerType } from "../api/servers";

// Only Vanilla + Fabric ship in the first version (docs/functional.md §3); the
// backend registry drives the actual list, but we label the ones we know.
const TYPE_LABELS: Record<string, string> = {
  vanilla: "Vanilla",
  fabric: "Fabric",
};

type Step = 0 | 1 | 2 | 3;

export default function CreateServer({ onCreated }: { onCreated: () => void }) {
  const [step, setStep] = useState<Step>(0);

  // Selections
  const [types, setTypes] = useState<ServerTypeInfo[]>([]);
  const [type, setType] = useState<ServerTypeInfo | null>(null);
  const [mcVersions, setMcVersions] = useState<string[]>([]);
  const [mcVersion, setMcVersion] = useState("");
  const [loaders, setLoaders] = useState<string[]>([]);
  const [loader, setLoader] = useState("");
  const [name, setName] = useState("");
  const [port, setPort] = useState(25565);
  const [memory, setMemory] = useState(2048);

  const [loadingList, setLoadingList] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fail = (e: unknown) =>
    setError(e instanceof ApiError ? e.message : String(e));

  // Step 0: server types.
  useEffect(() => {
    getServerTypes().then(setTypes).catch(fail);
  }, []);

  // Step 1: Minecraft versions for the chosen type.
  async function chooseType(t: ServerTypeInfo) {
    setType(t);
    setMcVersion("");
    setLoader("");
    setStep(1);
    setLoadingList(true);
    try {
      setMcVersions(await getMinecraftVersions(t.key));
    } catch (e) {
      fail(e);
    } finally {
      setLoadingList(false);
    }
  }

  // Step 2: loader builds (Fabric only), else jump to details.
  async function chooseVersion(v: string) {
    setMcVersion(v);
    if (!type?.needs_loader) {
      setStep(3);
      return;
    }
    setStep(2);
    setLoadingList(true);
    try {
      const ls = await getFabricLoaders(v);
      setLoaders(ls);
      setLoader(ls[0] ?? "");
    } catch (e) {
      fail(e);
    } finally {
      setLoadingList(false);
    }
  }

  async function submit() {
    if (!type) return;
    setSubmitting(true);
    setError(null);
    try {
      await createServer({
        name,
        type: type.key as ServerType,
        mc_version: mcVersion,
        loader_version: type.needs_loader ? loader : null,
        port,
        memory_mb: memory,
      });
      reset();
      onCreated();
    } catch (e) {
      fail(e);
    } finally {
      setSubmitting(false);
    }
  }

  function reset() {
    setStep(0);
    setType(null);
    setMcVersion("");
    setLoader("");
    setName("");
    setPort(25565);
    setMemory(2048);
  }

  const stepLabels = ["Type", "Version", "Loader", "Details"];

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-medium text-slate-200">New server</h2>
        {step > 0 && (
          <button
            onClick={reset}
            className="text-xs text-slate-400 hover:text-slate-200"
          >
            Start over
          </button>
        )}
      </div>

      {/* Step indicator */}
      <ol className="flex gap-2 text-xs">
        {stepLabels.map((label, i) => {
          // Loader step is skipped for loaderless types.
          const hidden = i === 2 && type !== null && !type.needs_loader;
          if (hidden) return null;
          return (
            <li
              key={label}
              className={`px-2 py-0.5 rounded-full ${
                i === step
                  ? "bg-emerald-600 text-white"
                  : i < step
                    ? "bg-slate-700 text-slate-300"
                    : "bg-slate-800 text-slate-500"
              }`}
            >
              {i + 1}. {label}
            </li>
          );
        })}
      </ol>

      <div className="rounded-lg border border-slate-800 p-4 space-y-3">
        {step === 0 && (
          <div className="space-y-2">
            <p className="text-sm text-slate-400">Choose a server type</p>
            <div className="flex gap-2">
              {types.map((t) => (
                <button
                  key={t.key}
                  onClick={() => chooseType(t)}
                  className="flex-1 rounded border border-slate-700 hover:border-emerald-500 bg-slate-800 px-4 py-3 text-sm font-medium"
                >
                  {TYPE_LABELS[t.key] ?? t.key}
                  {t.needs_loader && (
                    <span className="block text-xs text-slate-500 font-normal">
                      mod loader
                    </span>
                  )}
                </button>
              ))}
              {types.length === 0 && (
                <p className="text-sm text-slate-500">Loading types…</p>
              )}
            </div>
          </div>
        )}

        {step === 1 && (
          <label className="text-sm space-y-1 block">
            <span className="text-slate-400">Minecraft version</span>
            {loadingList ? (
              <p className="text-sm text-slate-500">Loading versions…</p>
            ) : (
              <select
                value={mcVersion}
                onChange={(e) => chooseVersion(e.target.value)}
                className="w-full bg-slate-800 rounded px-2 py-1"
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
        )}

        {step === 2 && (
          <label className="text-sm space-y-1 block">
            <span className="text-slate-400">Fabric loader build</span>
            {loadingList ? (
              <p className="text-sm text-slate-500">Loading loaders…</p>
            ) : (
              <select
                value={loader}
                onChange={(e) => setLoader(e.target.value)}
                className="w-full bg-slate-800 rounded px-2 py-1"
              >
                {loaders.map((l) => (
                  <option key={l} value={l}>
                    {l}
                  </option>
                ))}
              </select>
            )}
            <div className="pt-2">
              <button
                onClick={() => setStep(3)}
                disabled={!loader}
                className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 rounded px-3 py-1.5 text-sm font-medium"
              >
                Continue
              </button>
            </div>
          </label>
        )}

        {step === 3 && (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              submit();
            }}
            className="grid grid-cols-2 gap-3"
          >
            <p className="col-span-2 text-xs text-slate-500">
              {TYPE_LABELS[type!.key] ?? type!.key} · MC {mcVersion}
              {type!.needs_loader && ` · loader ${loader}`}
            </p>
            <label className="col-span-2 text-sm space-y-1">
              <span className="text-slate-400">Name</span>
              <input
                required
                autoFocus
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="w-full bg-slate-800 rounded px-2 py-1"
              />
            </label>
            <label className="text-sm space-y-1">
              <span className="text-slate-400">Port</span>
              <input
                type="number"
                value={port}
                onChange={(e) => setPort(Number(e.target.value))}
                className="w-full bg-slate-800 rounded px-2 py-1"
              />
            </label>
            <label className="text-sm space-y-1">
              <span className="text-slate-400">Memory (MB)</span>
              <input
                type="number"
                value={memory}
                onChange={(e) => setMemory(Number(e.target.value))}
                className="w-full bg-slate-800 rounded px-2 py-1"
              />
            </label>
            <div className="col-span-2">
              <button
                type="submit"
                disabled={submitting || !name}
                className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 rounded px-3 py-1.5 text-sm font-medium"
              >
                {submitting ? "Creating…" : "Create server"}
              </button>
            </div>
          </form>
        )}
      </div>

      {error && <p className="text-sm text-red-400">{error}</p>}
    </section>
  );
}
