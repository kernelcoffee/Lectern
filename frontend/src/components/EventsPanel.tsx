// Recent-events panel: the persisted timeline (starts/stops, crashes and
// auto-restarts, backup and schedule outcomes) that survives past the live
// console — so a 3am crash is still visible in the morning. Self-fetching:
// scoped to one server when `serverId` is given (Metrics tab), otherwise the
// cross-server feed with server names (Dashboard).

import { useEffect, useState } from "react";
import {
  getRecentEvents,
  getServerEvents,
  ServerEventWithName,
} from "../api/events";

const POLL_MS = 10000;

const KIND: Record<string, { label: string; dot: string }> = {
  started: { label: "Started", dot: "bg-emerald-500" },
  stopped: { label: "Stopped", dot: "bg-slate-500" },
  crashed: { label: "Crashed", dot: "bg-red-500" },
  crash_restart: { label: "Auto-restart", dot: "bg-amber-500" },
  crash_gave_up: { label: "Auto-restart gave up", dot: "bg-red-500" },
  backup_created: { label: "Backup created", dot: "bg-emerald-500" },
  backup_restored: { label: "Backup restored", dot: "bg-sky-500" },
  backup_failed: { label: "Backup failed", dot: "bg-red-500" },
  schedule_failed: { label: "Schedule failed", dot: "bg-amber-500" },
  mods_updated: { label: "Mods updated", dot: "bg-sky-500" },
};

function timeAgo(iso: string): string {
  const then = new Date(iso + "Z").getTime();
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  if (mins < 60 * 24) return `${Math.round(mins / 60)}h ago`;
  if (mins < 60 * 24 * 7) return `${Math.round(mins / (60 * 24))}d ago`;
  return new Date(iso + "Z").toLocaleDateString();
}

export default function EventsPanel({ serverId }: { serverId?: string }) {
  const [events, setEvents] = useState<ServerEventWithName[] | null>(null);

  useEffect(() => {
    let live = true;
    const tick = async () => {
      try {
        const rows = serverId
          ? (await getServerEvents(serverId)).map((e) => ({
              ...e,
              server_name: "",
            }))
          : await getRecentEvents();
        if (live) setEvents(rows);
      } catch {
        // Transient fetch errors just keep the previous list.
      }
    };
    tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      live = false;
      clearInterval(id);
    };
  }, [serverId]);

  return (
    <section className="rounded-lg border border-slate-800 overflow-hidden">
      <div className="border-b border-slate-800 bg-slate-950/50 px-4 py-3">
        <h3 className="text-sm font-medium text-slate-300">Recent events</h3>
      </div>

      {events === null || events.length === 0 ? (
        <p className="p-4 text-sm text-slate-500">
          Nothing yet — starts, stops, crashes, and backup results will show up
          here.
        </p>
      ) : (
        <ul className="divide-y divide-slate-800/60">
          {events.map((e) => {
            const kind = KIND[e.kind] ?? { label: e.kind, dot: "bg-slate-500" };
            return (
              <li
                key={e.id}
                className="flex items-baseline gap-2 px-4 py-2 text-sm"
              >
                <span
                  className={`h-2 w-2 shrink-0 self-center rounded-full ${kind.dot}`}
                />
                {!serverId && (
                  <span className="shrink-0 text-slate-400">
                    {e.server_name}
                  </span>
                )}
                <span className="text-slate-200">{kind.label}</span>
                {e.message && (
                  <span className="min-w-0 flex-1 truncate text-xs text-slate-500">
                    {e.message}
                  </span>
                )}
                <span
                  className="ml-auto shrink-0 text-xs text-slate-500"
                  title={new Date(e.created_at + "Z").toLocaleString()}
                >
                  {timeAgo(e.created_at)}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
