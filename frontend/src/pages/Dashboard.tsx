// Dashboard (Crafty-inspired): stat tiles up top, then the all-servers table
// with quick start/stop actions.
//
// Data flow:
//   * `servers` comes from App (shared with the sidebar); `onReload` refreshes
//     it after an action here.
//   * Stats (players) are fetched per *running* server on a 5s timer — the
//     backend endpoint is pull-based, so mounting the dashboard is what starts
//     polling and leaving it stops it.
//   * Install progress is polled per *installing* server on the same timer and
//     shown under the server name (same behaviour the old list page had).

import { useEffect, useState } from "react";
import { ApiError } from "../api/client";
import {
  getServerProgress,
  getServerStats,
  Server,
  serverAction,
  ServerStats,
  ServerStatus,
} from "../api/servers";
import { Route } from "../App";

const CHIP: Record<ServerStatus, string> = {
  installing: "bg-sky-500 text-slate-900",
  install_failed: "bg-red-500 text-slate-100",
  stopped: "bg-slate-600 text-slate-100",
  starting: "bg-amber-500 text-slate-900",
  running: "bg-emerald-500 text-slate-900",
  stopping: "bg-amber-500 text-slate-900",
  crashed: "bg-red-500 text-slate-100",
};

export default function Dashboard({
  servers,
  onReload,
  onNavigate,
}: {
  servers: Server[];
  onReload: () => Promise<void>;
  onNavigate: (r: Route) => void;
}) {
  const [stats, setStats] = useState<Record<string, ServerStats>>({});
  const [progress, setProgress] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null); // server id
  const [error, setError] = useState<string | null>(null);

  const runningIds = servers
    .filter((s) => s.status === "running")
    .map((s) => s.id)
    .join(",");
  const installingIds = servers
    .filter((s) => s.status === "installing")
    .map((s) => s.id)
    .join(",");
  const transientIds = servers
    .filter((s) => s.status === "starting" || s.status === "stopping")
    .map((s) => s.id)
    .join(",");

  // Poll stats for running servers + progress for installing ones.
  useEffect(() => {
    let cancelled = false;
    async function tick() {
      const statEntries = await Promise.all(
        runningIds
          .split(",")
          .filter(Boolean)
          .map(async (id) => {
            try {
              return [id, await getServerStats(id)] as const;
            } catch {
              return null;
            }
          }),
      );
      const progressEntries = await Promise.all(
        installingIds
          .split(",")
          .filter(Boolean)
          .map(async (id) => {
            try {
              const p = await getServerProgress(id);
              return [id, p.error ? `Failed: ${p.error}` : p.message] as const;
            } catch {
              return null;
            }
          }),
      );
      if (cancelled) return;
      setStats(Object.fromEntries(statEntries.filter((e) => e !== null)));
      setProgress(Object.fromEntries(progressEntries.filter((e) => e !== null)));
    }
    tick();
    const t = window.setInterval(tick, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(t);
    };
  }, [runningIds, installingIds]);

  // Follow start/stop transitions so chips settle without a manual refresh.
  useEffect(() => {
    if (!transientIds) return;
    const t = window.setInterval(onReload, 1500);
    return () => window.clearInterval(t);
  }, [transientIds, onReload]);

  async function quickAction(server: Server, action: "start" | "stop") {
    setBusy(server.id);
    setError(null);
    try {
      await serverAction(server.id, action);
      await onReload();
    } catch (e) {
      setError(
        `${server.name}: ${e instanceof ApiError ? e.message : String(e)}`,
      );
    } finally {
      setBusy(null);
    }
  }

  const running = servers.filter((s) => s.status === "running");
  const issues = servers.filter(
    (s) => s.status === "crashed" || s.status === "install_failed",
  );
  const playersOnline = running.reduce(
    (n, s) => n + (stats[s.id]?.ping?.online ?? 0),
    0,
  );
  const playersMax = running.reduce(
    (n, s) => n + (stats[s.id]?.ping?.max ?? 0),
    0,
  );

  return (
    <div className="max-w-5xl mx-auto p-6 space-y-6">
      <h2 className="text-xl font-semibold">Dashboard</h2>

      {/* Stat tiles */}
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
        <StatTile
          label="Servers running"
          value={`${running.length} / ${servers.length}`}
        />
        <StatTile
          label="Players online"
          value={running.length > 0 ? `${playersOnline} / ${playersMax}` : "—"}
        />
        <StatTile
          label="Needs attention"
          value={String(issues.length)}
          tone={issues.length > 0 ? "bad" : undefined}
        />
      </div>

      {/* All servers */}
      <section className="rounded-lg border border-slate-800 overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-slate-800 bg-slate-950/50">
          <h3 className="text-sm font-medium text-slate-300">All servers</h3>
          <button
            onClick={() => onNavigate({ view: "create" })}
            className="bg-emerald-600 hover:bg-emerald-500 rounded px-3 py-1.5 text-sm font-medium text-slate-900"
          >
            New server
          </button>
        </div>

        {servers.length === 0 ? (
          <div className="p-10 text-center space-y-1">
            <p className="text-slate-300">Welcome to Lectern!</p>
            <p className="text-sm text-slate-500">
              No servers yet — create your first one.
            </p>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-slate-500 border-b border-slate-800">
                <th className="px-4 py-2 font-medium">Server</th>
                <th className="px-4 py-2 font-medium">Version</th>
                <th className="px-4 py-2 font-medium">Port</th>
                <th className="px-4 py-2 font-medium">Players</th>
                <th className="px-4 py-2 font-medium">Status</th>
                <th className="px-4 py-2 font-medium text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {servers.map((s) => {
                const ping = stats[s.id]?.ping;
                const canStart =
                  s.status === "stopped" || s.status === "crashed";
                const canStop =
                  s.status === "running" || s.status === "starting";
                return (
                  <tr key={s.id} className="hover:bg-slate-800/40">
                    <td className="px-4 py-2.5">
                      <button
                        onClick={() => onNavigate({ view: "server", id: s.id })}
                        className="font-medium hover:text-emerald-400 text-left"
                      >
                        {s.name}
                      </button>
                      {s.status === "installing" && progress[s.id] && (
                        <p className="text-xs text-sky-400">{progress[s.id]}…</p>
                      )}
                    </td>
                    <td className="px-4 py-2.5 text-slate-400">
                      {s.type} {s.mc_version}
                    </td>
                    <td className="px-4 py-2.5 text-slate-400 tabular-nums">
                      {s.port}
                    </td>
                    <td className="px-4 py-2.5 text-slate-400 tabular-nums">
                      {s.status === "running" && ping
                        ? `${ping.online} / ${ping.max}`
                        : "—"}
                    </td>
                    <td className="px-4 py-2.5">
                      <span
                        className={`text-xs px-2 py-0.5 rounded-full ${CHIP[s.status]}`}
                      >
                        {s.status}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-right space-x-1.5">
                      <button
                        onClick={() => quickAction(s, "start")}
                        disabled={!canStart || busy !== null}
                        className="bg-emerald-700 hover:bg-emerald-600 disabled:opacity-30 rounded px-2.5 py-1 text-xs"
                        title="Start"
                      >
                        Start
                      </button>
                      <button
                        onClick={() => quickAction(s, "stop")}
                        disabled={!canStop || busy !== null}
                        className="bg-slate-700 hover:bg-slate-600 disabled:opacity-30 rounded px-2.5 py-1 text-xs"
                        title="Stop"
                      >
                        Stop
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>

      {error && <p className="text-sm text-red-400">{error}</p>}
    </div>
  );
}

function StatTile({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "bad";
}) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/50 px-4 py-3">
      <p className="text-xs text-slate-500">{label}</p>
      <p
        className={`text-2xl font-semibold ${
          tone === "bad" ? "text-red-400" : "text-slate-100"
        }`}
      >
        {value}
      </p>
    </div>
  );
}
