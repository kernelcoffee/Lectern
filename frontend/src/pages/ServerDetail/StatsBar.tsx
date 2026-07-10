// Live stats strip shown on the server detail page while the server runs.
//
// Workflow:
//   1. The parent (ServerDetail) mounts this component only when the server's
//      status is "running" — unmounting stops the polling automatically.
//   2. Every POLL_MS we call GET /api/servers/{id}/stats. The backend computes
//      the snapshot on request (psutil + a Server List Ping to localhost);
//      there is no server-side push for stats, unlike the console WebSocket.
//   3. `stats.ping` is null while the JVM is still booting (the process exists
//      but nothing listens on the game port yet) — render placeholders then.
//
// CPU note: the backend measures CPU between two consecutive polls, so the
// first reading after a start is always 0 — it becomes meaningful from the
// second tick onward.

import { useEffect, useState } from "react";
import { getServerStats, ServerStats } from "../../api/servers";

const POLL_MS = 5000;

/** "3721" seconds → "1h 2m"; keeps the strip compact. */
function formatUptime(totalSeconds: number): string {
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  const s = totalSeconds % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

export default function StatsBar({ serverId }: { serverId: string }) {
  const [stats, setStats] = useState<ServerStats | null>(null);

  useEffect(() => {
    let cancelled = false; // guards against setState after unmount

    async function poll() {
      try {
        const s = await getServerStats(serverId);
        if (!cancelled) setStats(s);
      } catch {
        // Transient failures (server stopping, backend restart) just skip a
        // tick; the next poll will recover.
      }
    }

    poll(); // immediate first reading, then on an interval
    const t = window.setInterval(poll, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(t);
    };
  }, [serverId]);

  // First poll not answered yet, or the process died between renders.
  if (!stats || !stats.running) return null;

  const ping = stats.ping;

  return (
    <div className="rounded-lg border border-slate-700 bg-slate-900/60 p-3 lg:min-w-[17rem]">
      <div className="mb-2 flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-slate-500">
        <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
        Live
      </div>
      <div className="grid grid-cols-2 gap-x-8 gap-y-2.5">
        <StatTile label="CPU" value={stats.cpu_percent !== null ? `${stats.cpu_percent}%` : "…"} />
        <StatTile
          label="Memory"
          value={stats.memory_mb !== null ? `${Math.round(stats.memory_mb)} MB` : "…"}
        />
        <StatTile
          label="Uptime"
          value={stats.uptime_seconds !== null ? formatUptime(stats.uptime_seconds) : "…"}
        />
        <StatTile label="Players" value={ping ? `${ping.online}/${ping.max}` : "…"} />
      </div>
      {ping ? (
        (ping.players.length > 0 || ping.version) && (
          <p className="mt-2 truncate text-[11px] text-slate-500" title={ping.players.join(", ")}>
            {ping.version && <span>v{ping.version}</span>}
            {ping.players.length > 0 && <span> · {ping.players.join(", ")}</span>}
          </p>
        )
      ) : (
        // Process is up but the game port doesn't answer yet (world loading).
        <p className="mt-2 text-[11px] text-slate-500">booting… (world loading)</p>
      )}
    </div>
  );
}

function StatTile({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[11px] text-slate-500">{label}</div>
      <div className="text-sm font-semibold tabular-nums text-slate-100">{value}</div>
    </div>
  );
}
