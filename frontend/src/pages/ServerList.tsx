import { useEffect, useRef, useState } from "react";
import {
  deleteServer,
  getServerProgress,
  listServers,
  Server,
  ServerStatus,
} from "../api/servers";
import { ApiError } from "../api/client";
import CreateServer from "./CreateServer";

const STATUS_STYLES: Record<ServerStatus, string> = {
  installing: "bg-sky-500 text-slate-900",
  install_failed: "bg-red-500 text-slate-100",
  stopped: "bg-slate-600 text-slate-100",
  starting: "bg-amber-500 text-slate-900",
  running: "bg-emerald-500 text-slate-900",
  stopping: "bg-amber-500 text-slate-900",
  crashed: "bg-red-500 text-slate-100",
};

export default function ServerList({ onOpen }: { onOpen: (id: string) => void }) {
  const [servers, setServers] = useState<Server[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // Live install-progress messages, keyed by server id.
  const [progress, setProgress] = useState<Record<string, string>>({});
  const timer = useRef<number | undefined>(undefined);

  async function refresh() {
    try {
      setServers(await listServers());
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  // While any server is installing, poll the list + per-server progress so the
  // record's status flips to `stopped` (and the step message updates) live.
  const installing = servers.filter((s) => s.status === "installing");
  useEffect(() => {
    if (installing.length === 0) {
      window.clearInterval(timer.current);
      return;
    }
    timer.current = window.setInterval(async () => {
      await refresh();
      const entries = await Promise.all(
        installing.map(async (s) => {
          try {
            const p = await getServerProgress(s.id);
            return [s.id, p.error ? `Failed: ${p.error}` : p.message] as const;
          } catch {
            return [s.id, ""] as const;
          }
        }),
      );
      setProgress(Object.fromEntries(entries));
    }, 1500);
    return () => window.clearInterval(timer.current);
    // Re-arm when the set of installing ids changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [installing.map((s) => s.id).join(",")]);

  async function onDelete(id: string) {
    try {
      await deleteServer(id);
      await refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  return (
    <div className="max-w-3xl mx-auto p-6 space-y-8">
      <section className="space-y-3">
        <h2 className="text-lg font-medium text-slate-200">Servers</h2>
        {loading ? (
          <p className="text-slate-500 text-sm">Loading…</p>
        ) : servers.length === 0 ? (
          <p className="text-slate-500 text-sm">No servers yet. Create one below.</p>
        ) : (
          <ul className="divide-y divide-slate-800 rounded-lg border border-slate-800">
            {servers.map((s) => (
              <li key={s.id} className="flex items-center gap-3 p-3">
                <span
                  className={`text-xs px-2 py-0.5 rounded-full ${STATUS_STYLES[s.status]}`}
                >
                  {s.status}
                </span>
                <button
                  onClick={() => onOpen(s.id)}
                  className="flex-1 min-w-0 text-left group"
                >
                  <div className="font-medium truncate group-hover:text-emerald-400">
                    {s.name}
                  </div>
                  <div className="text-xs text-slate-400">
                    {s.type} · MC {s.mc_version}
                    {s.loader_version && ` · ${s.loader_version}`} · :{s.port} ·{" "}
                    {s.memory_mb} MB
                  </div>
                  {s.status === "installing" && progress[s.id] && (
                    <div className="text-xs text-sky-400 mt-0.5">
                      {progress[s.id]}…
                    </div>
                  )}
                </button>
                <button
                  onClick={() => onDelete(s.id)}
                  className="text-xs text-red-400 hover:text-red-300"
                >
                  Delete
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <CreateServer onCreated={refresh} />

      {error && <p className="text-sm text-red-400">{error}</p>}
    </div>
  );
}
