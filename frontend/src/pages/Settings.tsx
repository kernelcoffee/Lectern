// Lectern settings page — edit the app-level tunables (upload caps, create-form
// default memory) that would otherwise only be env vars / config. Values are
// stored server-side (Setting table) layered over the env defaults; edited
// fields are sent on Save, then the response replaces the source of truth.

import { useEffect, useState } from "react";
import { errorMessage } from "../api/client";
import { AppSetting, getSettings, updateSettings } from "../api/settings";
import { useToast } from "../components/Toasts";

/** Group settings by category, preserving first-seen category order. */
function groupByCategory(settings: AppSetting[]): [string, AppSetting[]][] {
  const groups = new Map<string, AppSetting[]>();
  for (const s of settings) {
    const list = groups.get(s.category) ?? [];
    list.push(s);
    groups.set(s.category, list);
  }
  return [...groups.entries()];
}

export default function Settings() {
  const [settings, setSettings] = useState<AppSetting[] | null>(null);
  const [edits, setEdits] = useState<Record<string, number>>({});
  const [busy, setBusy] = useState(false);
  // Load failures only — save outcomes go through toasts.
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();

  useEffect(() => {
    getSettings()
      .then(setSettings)
      .catch((e) => setError(errorMessage(e)));
  }, []);

  const dirty = Object.keys(edits).length > 0;

  async function save() {
    setBusy(true);
    try {
      const updated = await updateSettings(edits);
      setSettings(updated);
      setEdits({});
      toast.success("Settings saved.");
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="p-4 sm:p-6 space-y-5">
      <div>
        <h2 className="text-xl font-semibold">Settings</h2>
        <p className="text-sm text-slate-500">
          App-level limits and defaults. Stored on the server; they take effect
          immediately.
        </p>
      </div>

      {error && <p className="text-sm text-red-400">{error}</p>}

      {settings === null ? (
        <p className="text-sm text-slate-500">Loading…</p>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (dirty) save();
          }}
          className="max-w-2xl space-y-6"
        >
          {groupByCategory(settings).map(([category, items]) => (
            <fieldset
              key={category}
              className="space-y-4 rounded-lg border border-slate-800 p-5"
            >
              <legend className="px-1 text-xs font-semibold uppercase tracking-wider text-slate-400">
                {category}
              </legend>
              {items.map((s) => {
                const value = edits[s.key] ?? s.value;
                return (
                  <div
                    key={s.key}
                    className="grid gap-1 sm:grid-cols-[1fr_auto] sm:items-start"
                  >
                    <div className="min-w-0">
                      <label htmlFor={s.key} className="text-sm text-slate-200">
                        {s.label}
                      </label>
                      <p className="text-xs text-slate-500">{s.help}</p>
                      <p className="text-[11px] text-slate-600">
                        Allowed: {s.min.toLocaleString()}–{s.max.toLocaleString()}{" "}
                        {s.unit}
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      <input
                        id={s.key}
                        type="number"
                        min={s.min}
                        max={s.max}
                        value={value}
                        onChange={(e) => {
                          const n = Number(e.target.value);
                          setEdits((prev) => {
                            const next = { ...prev };
                            if (n === s.value) delete next[s.key];
                            else next[s.key] = n;
                            return next;
                          });
                        }}
                        className="w-32 rounded bg-slate-800 border border-slate-700 px-2.5 py-1.5 text-sm text-right tabular-nums"
                      />
                      <span className="w-8 text-xs text-slate-500">{s.unit}</span>
                    </div>
                  </div>
                );
              })}
            </fieldset>
          ))}

          <div className="flex items-center gap-2">
            <button
              type="submit"
              disabled={!dirty || busy}
              className="rounded bg-emerald-600 px-4 py-1.5 text-sm font-medium text-slate-900 hover:bg-emerald-500 disabled:opacity-40"
            >
              {busy ? "Saving…" : "Save changes"}
            </button>
            {dirty && (
              <button
                type="button"
                onClick={() => setEdits({})}
                className="rounded bg-slate-800 px-4 py-1.5 text-sm hover:bg-slate-700"
              >
                Discard
              </button>
            )}
          </div>
        </form>
      )}
    </div>
  );
}
