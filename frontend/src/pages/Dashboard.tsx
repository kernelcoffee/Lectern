// Dashboard (Crafty-inspired): stat tiles up top, then the all-servers table
// with quick start/stop actions.
//
// Data flow:
//   * `servers` comes from App (shared with the sidebar); `onReload` refreshes
//     it after an action here.
//   * Stats (players) are fetched per *running* server on a 5s timer — the
//     backend endpoint is pull-based, so mounting the dashboard is what starts
//     polling and leaving it stops it.
//   * Install progress streams over WebSocket per *installing* row
//     (useInstallProgress) — no polling; the socket closes itself when done.

import { useEffect, useState } from "react";
import { errorMessage } from "../api/client";
import {
  getServerStats,
  Server,
  serverAction,
  ServerStats,
} from "../api/servers";
import { Route } from "../App";
import EventsPanel from "../components/EventsPanel";
import { STATUS_CHIP } from "../components/status";
import { useToast } from "../components/Toasts";
import { useInstallProgress } from "../hooks/useInstallProgress";

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
  const [busy, setBusy] = useState<string | null>(null); // server id
  const toast = useToast();

  const runningIds = servers
    .filter((s) => s.status === "running")
    .map((s) => s.id)
    .join(",");
  const transientIds = servers
    .filter((s) => s.status === "starting" || s.status === "stopping")
    .map((s) => s.id)
    .join(",");

  // Poll stats for running servers (the endpoint is pull-based).
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
      if (cancelled) return;
      setStats(Object.fromEntries(statEntries.filter((e) => e !== null)));
    }
    tick();
    const t = window.setInterval(tick, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(t);
    };
  }, [runningIds]);

  // Follow start/stop transitions so chips settle without a manual refresh.
  useEffect(() => {
    if (!transientIds) return;
    const t = window.setInterval(onReload, 1500);
    return () => window.clearInterval(t);
  }, [transientIds, onReload]);

  async function quickAction(server: Server, action: "start" | "stop") {
    setBusy(server.id);
    try {
      await serverAction(server.id, action);
      await onReload();
    } catch (e) {
      toast.error(
        `${server.name}: ${errorMessage(e)}`,
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
    <div className="p-4 sm:p-6 space-y-6">
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
              {/* Version/Port/Players collapse into a subline under the name
                  on narrow screens — a 6-column table can't fit a phone. */}
              <tr className="text-left text-xs text-slate-500 border-b border-slate-800">
                <th className="px-4 py-2 font-medium">Server</th>
                <th className="hidden md:table-cell px-4 py-2 font-medium">Version</th>
                <th className="hidden md:table-cell px-4 py-2 font-medium">Port</th>
                <th className="hidden sm:table-cell px-4 py-2 font-medium">Players</th>
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
                      {s.status === "installing" && (
                        <InstallProgressLine serverId={s.id} />
                      )}
                      <p className="md:hidden text-xs text-slate-500">
                        {s.type} {s.mc_version} · :{s.port}
                      </p>
                    </td>
                    <td className="hidden md:table-cell px-4 py-2.5 text-slate-400">
                      {s.type} {s.mc_version}
                    </td>
                    <td className="hidden md:table-cell px-4 py-2.5 text-slate-400 tabular-nums">
                      {s.port}
                    </td>
                    <td className="hidden sm:table-cell px-4 py-2.5 text-slate-400 tabular-nums">
                      {s.status === "running" && ping
                        ? `${ping.online} / ${ping.max}`
                        : "—"}
                    </td>
                    <td className="px-4 py-2.5">
                      <span
                        className={`text-xs px-2 py-0.5 rounded-full ${STATUS_CHIP[s.status]}`}
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

      <EventsPanel />
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

/** Live install step under the server name (only mounted while installing —
 *  mounting opens the WS, unmounting closes it). */
function InstallProgressLine({ serverId }: { serverId: string }) {
  const progress = useInstallProgress(serverId);
  if (!progress) return null;
  return (
    <p className="text-xs text-sky-400">
      {progress.error
        ? `Failed: ${progress.error}`
        : `${progress.message.replace(/…$/, "")}…`}
    </p>
  );
}
