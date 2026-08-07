// Proxy tab (Velocity servers): link Lectern servers behind the proxy.
//
// The checked servers become velocity.toml's [servers] registry; their order
// is the try order and the FIRST linked server is where new players land
// ("default"). Linking flips the backend to online-mode=false and, on
// Fabric/Quilt, auto-installs FabricProxy-Lite with the proxy's forwarding
// secret; the per-row hint explains what identity forwarding each backend
// type gets. Everything applies at the next start of the affected servers.

import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "../../api/client";
import {
  getProxyLinks,
  ProxyCandidate,
  ProxyPayload,
  setProxyLinks,
} from "../../api/proxy";
import { useToast } from "../../components/Toasts";

const FORWARDING_HINT: Record<ProxyCandidate["forwarding"], { text: string; tone: "ok" | "warn" }> = {
  mod: {
    text: "identity forwarding via FabricProxy-Lite (installed automatically)",
    tone: "ok",
  },
  manual: {
    text: "needs a proxy-compat mod installed manually — players get offline UUIDs until then",
    tone: "warn",
  },
  none: {
    text: "vanilla can't verify proxied players — offline UUIDs; whitelist/ops won't match",
    tone: "warn",
  },
};

export default function ProxyTab({ serverId }: { serverId: string }) {
  const [payload, setPayload] = useState<ProxyPayload | null>(null);
  // Ordered ids of linked servers (order = try order, first = default).
  const [linked, setLinked] = useState<string[]>([]);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  // Load failures only — actions report through toasts.
  const [error, setError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [moved, setMoved] = useState<ProxyPayload["moved"]>([]);
  const toast = useToast();

  const load = useCallback(async () => {
    try {
      const data = await getProxyLinks(serverId);
      setPayload(data);
      // Reconstruct the linked order from try + link addresses.
      const byName = new Map(data.links.map((l) => [l.name, l.server_id]));
      const ordered = data.try
        .map((name) => byName.get(name))
        .filter((id): id is string => !!id);
      const unordered = data.links
        .map((l) => l.server_id)
        .filter((id): id is string => !!id && !ordered.includes(id));
      setLinked([...ordered, ...unordered]);
      setDirty(false);
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, [serverId]);

  useEffect(() => {
    load();
  }, [load]);

  function toggle(id: string) {
    setLinked((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );
    setDirty(true);
  }

  function makeDefault(id: string) {
    setLinked((prev) => [id, ...prev.filter((x) => x !== id)]);
    setDirty(true);
  }

  async function save() {
    setBusy(true);
    try {
      const result = await setProxyLinks(serverId, linked);
      setPayload(result);
      setWarnings(result.warnings ?? []);
      setMoved(result.moved ?? []);
      setDirty(false);
      toast.success("Proxy links saved — applies at the next start of each server.");
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  if (error) return <p className="text-sm text-red-400">{error}</p>;
  if (!payload) return <p className="text-sm text-slate-500">Loading…</p>;

  return (
    <section className="space-y-4">
      <div>
        <h3 className="text-sm font-medium text-slate-300">Linked servers</h3>
        <p className="text-xs text-slate-500">
          Players connect to this proxy's port and land on the{" "}
          <span className="text-slate-400">default</span> server; linked servers
          run with <code>online-mode=false</code> (the proxy authenticates).
          Changes apply at the next start of the proxy and each affected server.
        </p>
      </div>

      {payload.candidates.length === 0 ? (
        <p className="text-sm text-slate-500">
          No servers to link yet — create a game server first.
        </p>
      ) : (
        <ul className="divide-y divide-slate-800 rounded-lg border border-slate-800">
          {payload.candidates.map((c) => {
            const isLinked = linked.includes(c.server_id);
            const isDefault = linked[0] === c.server_id;
            const hint = FORWARDING_HINT[c.forwarding];
            return (
              <li
                key={c.server_id}
                className="flex flex-wrap items-center gap-x-3 gap-y-2 p-3"
              >
                <label className="flex w-full items-center gap-3 sm:w-auto sm:flex-1 min-w-0">
                  <input
                    type="checkbox"
                    checked={isLinked}
                    onChange={() => toggle(c.server_id)}
                    disabled={busy}
                  />
                  <span className="min-w-0">
                    <span className="flex items-center gap-2 text-sm">
                      <span className="truncate">{c.name}</span>
                      <span className="text-xs text-slate-500">
                        {c.type} · :{c.port}
                      </span>
                      {isDefault && (
                        <span className="rounded bg-emerald-700/60 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-emerald-200">
                          default
                        </span>
                      )}
                    </span>
                    <span
                      className={
                        "block text-xs " +
                        (hint.tone === "warn" ? "text-amber-400/90" : "text-slate-500")
                      }
                    >
                      {hint.text}
                    </span>
                  </span>
                </label>
                {isLinked && !isDefault && (
                  <button
                    onClick={() => makeDefault(c.server_id)}
                    disabled={busy}
                    className="rounded bg-slate-700 px-2.5 py-1 text-xs hover:bg-slate-600 disabled:opacity-50"
                  >
                    Make default
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}

      <div className="flex items-center gap-3">
        <button
          onClick={save}
          disabled={!dirty || busy}
          className="rounded bg-emerald-600 px-4 py-1.5 text-sm font-medium text-slate-900 hover:bg-emerald-500 disabled:opacity-40"
        >
          {busy ? "Saving…" : "Save links"}
        </button>
        {dirty && !busy && (
          <span className="text-xs text-amber-400">unsaved changes</span>
        )}
      </div>

      {(moved?.length ?? 0) > 0 && (
        <ul className="space-y-1 rounded-lg border border-sky-800/60 bg-sky-950/30 p-3">
          {moved!.map((m) => (
            <li key={m.server_id} className="text-xs text-sky-200">
              {m.name} was using the proxy's port — moved to internal port{" "}
              <span className="font-mono">{m.to}</span> (was {m.from}).
            </li>
          ))}
        </ul>
      )}

      {warnings.length > 0 && (
        <ul className="space-y-1 rounded-lg border border-amber-700/60 bg-amber-950/30 p-3">
          {warnings.map((w) => (
            <li key={w} className="text-xs text-amber-200">
              {w}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
