// Schedule tab (M10) — recurring actions for this server.
//
// A row = one schedule: an action (start/stop/restart/backup/console command)
// on a recurring time. Non-technical users pick a frequency (every day / week
// / hour / N hours) and pick times from dropdowns — the raw cron expression is
// generated for them (see cron.ts) and only shown as a muted hint. Power users
// can switch the frequency to "Advanced" and type a crontab directly. The
// backend validates the expression, computes `next_run`, and toggling a row
// off keeps it but stops the job.

import { ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { ApiError } from "../../api/client";
import {
  createSchedule,
  deleteSchedule,
  listSchedules,
  Schedule,
  ScheduleAction,
  updateSchedule,
} from "../../api/schedules";
import {
  buildCron,
  CronBuilderState,
  DEFAULT_BUILDER,
  describeCron,
  Frequency,
  WEEKDAYS,
} from "./cron";

const ACTIONS: { value: ScheduleAction; label: string }[] = [
  { value: "restart", label: "Restart server" },
  { value: "start", label: "Start server" },
  { value: "stop", label: "Stop server" },
  { value: "backup", label: "Back up server" },
  { value: "command", label: "Run console command" },
];

const FREQUENCIES: { value: Frequency; label: string }[] = [
  { value: "daily", label: "Every day" },
  { value: "weekly", label: "Certain days" },
  { value: "hourly", label: "Every hour" },
  { value: "everyNHours", label: "Every few hours" },
  { value: "advanced", label: "Advanced (cron)" },
];

export default function ScheduleTab({ serverId }: { serverId: string }) {
  const [schedules, setSchedules] = useState<Schedule[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // schedule id or "add"
  const [error, setError] = useState<string | null>(null);

  // Add-form state.
  const [action, setAction] = useState<ScheduleAction>("restart");
  const [command, setCommand] = useState("");
  const [oneTime, setOneTime] = useState(false);
  const [builder, setBuilder] = useState<CronBuilderState>(DEFAULT_BUILDER);

  const cron = useMemo(() => buildCron(builder), [builder]);

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
      if (!cron) throw new ApiError(0, "Pick when this schedule should run");
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
    run(s.id, () => updateSchedule(serverId, s.id, { enabled: !s.enabled }).then(() => {}));

  const remove = (s: Schedule) =>
    run(s.id, () => deleteSchedule(serverId, s.id));

  const set = (patch: Partial<CronBuilderState>) =>
    setBuilder((b) => ({ ...b, ...patch }));

  const commandMissing = action === "command" && !command.trim();

  return (
    <section className="space-y-4">
      {/* Add form */}
      <div className="space-y-4 rounded-lg border border-slate-800 p-4">
        <h3 className="text-sm font-semibold text-slate-200">New schedule</h3>

        {/* What + (optional) command */}
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Do this">
            <select
              value={action}
              onChange={(e) => setAction(e.target.value as ScheduleAction)}
              className={selectCls}
            >
              {ACTIONS.map((a) => (
                <option key={a.value} value={a.value}>
                  {a.label}
                </option>
              ))}
            </select>
          </Field>

          {action === "command" && (
            <Field label="Command" grow>
              <input
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                placeholder="say Server restarting in 5 minutes!"
                className={inputCls}
              />
            </Field>
          )}
        </div>

        {/* When */}
        <div className="flex flex-wrap items-end gap-3">
          <Field label="How often">
            <select
              value={builder.frequency}
              onChange={(e) => set({ frequency: e.target.value as Frequency })}
              className={selectCls}
            >
              {FREQUENCIES.map((f) => (
                <option key={f.value} value={f.value}>
                  {f.label}
                </option>
              ))}
            </select>
          </Field>

          {(builder.frequency === "daily" || builder.frequency === "weekly") && (
            <Field label="At">
              <input
                type="time"
                value={builder.time}
                onChange={(e) => set({ time: e.target.value })}
                className={inputCls}
              />
            </Field>
          )}

          {builder.frequency === "hourly" && (
            <Field label="At minute">
              <input
                type="number"
                min={0}
                max={59}
                value={builder.minute}
                onChange={(e) => set({ minute: Number(e.target.value) })}
                className={`${inputCls} w-24`}
              />
            </Field>
          )}

          {builder.frequency === "everyNHours" && (
            <Field label="Every … hours">
              <input
                type="number"
                min={1}
                max={23}
                value={builder.everyN}
                onChange={(e) => set({ everyN: Number(e.target.value) })}
                className={`${inputCls} w-24`}
              />
            </Field>
          )}

          {builder.frequency === "advanced" && (
            <Field label="Cron (min hour day month weekday)" grow>
              <input
                value={builder.raw}
                onChange={(e) => set({ raw: e.target.value })}
                placeholder="0 4 * * *"
                className={`${inputCls} font-mono`}
              />
            </Field>
          )}
        </div>

        {builder.frequency === "weekly" && (
          <div className="flex flex-wrap gap-1.5">
            {WEEKDAYS.map((d) => {
              const on = builder.weekdays.includes(d.key);
              return (
                <button
                  key={d.key}
                  type="button"
                  onClick={() =>
                    set({
                      weekdays: on
                        ? builder.weekdays.filter((k) => k !== d.key)
                        : [...builder.weekdays, d.key],
                    })
                  }
                  className={
                    "rounded px-2.5 py-1 text-xs " +
                    (on
                      ? "bg-emerald-600 text-slate-900"
                      : "bg-slate-800 text-slate-300 hover:bg-slate-700")
                  }
                >
                  {d.short}
                </button>
              );
            })}
          </div>
        )}

        {/* Live preview + submit */}
        <div className="flex flex-wrap items-center gap-3">
          <p className="flex-1 text-xs text-slate-400">
            {cron ? (
              <>
                Runs{" "}
                <span className="text-slate-200">
                  {describeCron(cron).toLowerCase()}
                </span>
                {oneTime && " (once)"}
                <code className="ml-2 text-slate-600">{cron}</code>
              </>
            ) : (
              <span className="text-amber-400">
                Pick at least one day for this schedule.
              </span>
            )}
          </p>
          <label className="flex items-center gap-1.5 text-xs text-slate-300">
            <input
              type="checkbox"
              checked={oneTime}
              onChange={(e) => setOneTime(e.target.checked)}
            />
            run once, then remove
          </label>
          <button
            onClick={add}
            disabled={busy !== null || !cron || commandMissing}
            className="rounded bg-emerald-600 px-3 py-1.5 text-sm font-medium text-slate-900 hover:bg-emerald-500 disabled:opacity-50"
          >
            {busy === "add" ? "Adding…" : "Add schedule"}
          </button>
        </div>
        <p className="text-xs text-slate-500">
          Times use the backend host's local clock.
        </p>
      </div>

      {error && <p className="text-sm text-red-400">{error}</p>}

      {/* Rows */}
      {!schedules ? (
        <p className="text-sm text-slate-500">Loading…</p>
      ) : schedules.length === 0 ? (
        <p className="text-sm text-slate-500">
          No schedules yet — a nightly restart and a daily backup are the usual
          first two.
        </p>
      ) : (
        <ul className="divide-y divide-slate-800 rounded-lg border border-slate-800">
          {schedules.map((s) => (
            <li key={s.id} className="flex items-center gap-3 p-3">
              <div className="flex-1 min-w-0">
                <p className="truncate text-sm">
                  {ACTIONS.find((a) => a.value === s.action)?.label ?? s.action}
                  {s.action === "command" && s.command && (
                    <code className="ml-2 text-xs text-slate-400">{s.command}</code>
                  )}
                  {s.one_time && (
                    <span className="ml-2 rounded bg-slate-800 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-slate-400">
                      once
                    </span>
                  )}
                </p>
                <p className="text-xs text-slate-500">
                  {describeCron(s.cron)}
                  {" · "}
                  {s.enabled && s.next_run
                    ? `next ${new Date(s.next_run).toLocaleString()}`
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

const selectCls =
  "mt-1 block w-44 rounded bg-slate-800 px-2 py-1.5 text-sm text-slate-100";
const inputCls =
  "mt-1 block w-full min-w-[8rem] rounded bg-slate-800 px-2 py-1.5 text-sm text-slate-100 placeholder:text-slate-600";

function Field({
  label,
  grow,
  children,
}: {
  label: string;
  grow?: boolean;
  children: ReactNode;
}) {
  return (
    <label className={`text-xs text-slate-400 ${grow ? "flex-1 min-w-[16rem]" : ""}`}>
      {label}
      {children}
    </label>
  );
}
