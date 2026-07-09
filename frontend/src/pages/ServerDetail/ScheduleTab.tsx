// Schedule tab (M10) — recurring cron actions for this server.
//
// A row = one cron schedule: action (start/stop/restart/backup/console
// command), a 5-field crontab, optional one-time flag. The backend validates
// the cron on every write (422 messages surface verbatim) and computes
// `next_run`; toggling a row off keeps it but stops the job. Times are the
// backend host's local time — the hint under the form says so.

import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../../api/client";
import {
  createSchedule,
  deleteSchedule,
  listSchedules,
  Schedule,
  ScheduleAction,
  updateSchedule,
} from "../../api/schedules";

const ACTIONS: { value: ScheduleAction; label: string }[] = [
  { value: "restart", label: "Restart" },
  { value: "start", label: "Start" },
  { value: "stop", label: "Stop" },
  { value: "backup", label: "Backup" },
  { value: "command", label: "Console command" },
];

// A few starting points so users don't have to remember cron syntax.
const CRON_PRESETS: { label: string; cron: string }[] = [
  { label: "Every day at 04:00", cron: "0 4 * * *" },
  { label: "Every 6 hours", cron: "0 */6 * * *" },
  { label: "Sundays at 03:00", cron: "0 3 * * sun" },
];

export default function ScheduleTab({ serverId }: { serverId: string }) {
  const [schedules, setSchedules] = useState<Schedule[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // schedule id or "add"
  const [error, setError] = useState<string | null>(null);

  // Add-form state.
  const [action, setAction] = useState<ScheduleAction>("restart");
  const [cron, setCron] = useState("0 4 * * *");
  const [command, setCommand] = useState("");
  const [oneTime, setOneTime] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setSchedules(await listSchedules(serverId));
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, [serverId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function run(key: string, fn: () => Promise<void>) {
    setBusy(key);
    setError(null);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const add = () =>
    run("add", async () => {
      await createSchedule(serverId, {
        action,
        cron,
        command: action === "command" ? command : null,
        one_time: oneTime,
      });
      setCommand("");
      setOneTime(false);
    });

  const toggle = (s: Schedule) =>
    run(s.id, async () => {
      await updateSchedule(serverId, s.id, { enabled: !s.enabled });
    });

  const remove = (s: Schedule) =>
    run(s.id, async () => {
      await deleteSchedule(serverId, s.id);
    });

  return (
    <section className="space-y-4">
      {/* Add form */}
      <div className="space-y-3 rounded-lg border border-slate-800 p-4">
        <h3 className="text-sm font-semibold text-slate-200">New schedule</h3>
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-xs text-slate-400">
            Action
            <select
              value={action}
              onChange={(e) => setAction(e.target.value as ScheduleAction)}
              className="mt-1 block w-44 rounded bg-slate-800 px-2 py-1.5 text-sm text-slate-100"
            >
              {ACTIONS.map((a) => (
                <option key={a.value} value={a.value}>
                  {a.label}
                </option>
              ))}
            </select>
          </label>

          {action === "command" && (
            <label className="text-xs text-slate-400">
              Command
              <input
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                placeholder="say Server restarting in 5 minutes!"
                className="mt-1 block w-72 rounded bg-slate-800 px-2 py-1.5 text-sm text-slate-100 placeholder:text-slate-600"
              />
            </label>
          )}

          <label className="text-xs text-slate-400">
            Cron (min hour day month weekday)
            <input
              value={cron}
              onChange={(e) => setCron(e.target.value)}
              placeholder="0 4 * * *"
              className="mt-1 block w-44 rounded bg-slate-800 px-2 py-1.5 font-mono text-sm text-slate-100 placeholder:text-slate-600"
            />
          </label>

          <label className="flex items-center gap-1.5 pb-2 text-xs text-slate-300">
            <input
              type="checkbox"
              checked={oneTime}
              onChange={(e) => setOneTime(e.target.checked)}
            />
            run once, then delete
          </label>

          <button
            onClick={add}
            disabled={busy !== null || (action === "command" && !command.trim())}
            className="rounded bg-emerald-600 px-3 py-1.5 text-sm font-medium text-slate-900 hover:bg-emerald-500 disabled:opacity-50"
          >
            {busy === "add" ? "Adding…" : "Add schedule"}
          </button>
        </div>
        <p className="text-xs text-slate-500">
          Presets:{" "}
          {CRON_PRESETS.map((p, i) => (
            <span key={p.cron}>
              {i > 0 && " · "}
              <button
                onClick={() => setCron(p.cron)}
                className="text-sky-400 hover:text-sky-300"
              >
                {p.label}
              </button>{" "}
              <code className="text-slate-600">{p.cron}</code>
            </span>
          ))}
          . Times are the backend host's local time.
        </p>
      </div>

      {error && <p className="text-sm text-red-400">{error}</p>}

      {/* Rows */}
      {!schedules ? (
        <p className="text-sm text-slate-500">Loading…</p>
      ) : schedules.length === 0 ? (
        <p className="text-sm text-slate-500">
          No schedules yet — nightly restarts and backups are the usual first
          two.
        </p>
      ) : (
        <ul className="divide-y divide-slate-800 rounded-lg border border-slate-800">
          {schedules.map((s) => (
            <li key={s.id} className="flex items-center gap-3 p-3">
              <div className="flex-1 min-w-0">
                <p className="text-sm truncate">
                  {ACTIONS.find((a) => a.value === s.action)?.label ?? s.action}
                  {s.action === "command" && s.command && (
                    <code className="ml-2 text-xs text-slate-400">
                      {s.command}
                    </code>
                  )}
                  {s.one_time && (
                    <span className="ml-2 rounded bg-slate-800 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-slate-400">
                      once
                    </span>
                  )}
                </p>
                <p className="text-xs text-slate-500">
                  <code>{s.cron}</code>
                  {" · "}
                  {s.enabled && s.next_run
                    ? `next: ${new Date(s.next_run).toLocaleString()}`
                    : "disabled"}
                </p>
              </div>
              <button
                onClick={() => toggle(s)}
                disabled={busy !== null}
                className={
                  "rounded px-2.5 py-1 text-xs disabled:opacity-50 " +
                  (s.enabled
                    ? "bg-slate-700 hover:bg-slate-600"
                    : "bg-emerald-700 hover:bg-emerald-600")
                }
              >
                {busy === s.id ? "…" : s.enabled ? "Disable" : "Enable"}
              </button>
              <button
                onClick={() => remove(s)}
                disabled={busy !== null}
                className="rounded bg-red-900/60 px-2.5 py-1 text-xs text-red-200 hover:bg-red-800 disabled:opacity-50"
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
