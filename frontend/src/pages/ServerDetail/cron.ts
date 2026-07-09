// Human-friendly cron building + description for the Schedule tab.
//
// Non-technical users pick a frequency (daily / weekly / hourly / every N
// hours) and we generate the 5-field crontab the backend expects; power users
// can still drop to a raw expression ("advanced"). `describeCron` turns any
// expression back into plain English for the row list and the live preview,
// falling back to the raw string for anything it doesn't recognise.
//
// IMPORTANT: weekdays are emitted as NAMES (mon…sun), never numbers. The
// backend's APScheduler `from_crontab` maps 0→Monday (not the Unix 0→Sunday)
// and rejects 7, so numeric day-of-week would fire on the wrong day; names are
// unambiguous and validated the same on both sides.

export type Frequency = "daily" | "weekly" | "hourly" | "everyNHours" | "advanced";

// Monday-first for display; `key` is what goes into the cron expression.
export const WEEKDAYS: { key: string; short: string; long: string }[] = [
  { key: "mon", short: "Mon", long: "Monday" },
  { key: "tue", short: "Tue", long: "Tuesday" },
  { key: "wed", short: "Wed", long: "Wednesday" },
  { key: "thu", short: "Thu", long: "Thursday" },
  { key: "fri", short: "Fri", long: "Friday" },
  { key: "sat", short: "Sat", long: "Saturday" },
  { key: "sun", short: "Sun", long: "Sunday" },
];

const WEEKDAY_ORDER = WEEKDAYS.map((d) => d.key);

export interface CronBuilderState {
  frequency: Frequency;
  time: string; // "HH:MM" — daily / weekly
  weekdays: string[]; // subset of WEEKDAY keys — weekly
  minute: number; // 0–59 — hourly
  everyN: number; // 1–23 — every N hours
  raw: string; // advanced
}

export const DEFAULT_BUILDER: CronBuilderState = {
  frequency: "daily",
  time: "04:00",
  weekdays: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
  minute: 0,
  everyN: 6,
  raw: "0 4 * * *",
};

function parseTime(time: string): { h: number; m: number } {
  const [h, m] = time.split(":").map((n) => parseInt(n, 10));
  return { h: Number.isFinite(h) ? h : 0, m: Number.isFinite(m) ? m : 0 };
}

/** Sort weekday keys Monday-first so the expression is stable/readable. */
function orderDays(days: string[]): string[] {
  return [...new Set(days)].sort(
    (a, b) => WEEKDAY_ORDER.indexOf(a) - WEEKDAY_ORDER.indexOf(b),
  );
}

/** Build a 5-field crontab from the builder state (or `null` if the current
 *  selection can't form a valid schedule, e.g. weekly with no days). */
export function buildCron(s: CronBuilderState): string | null {
  switch (s.frequency) {
    case "daily": {
      const { h, m } = parseTime(s.time);
      return `${m} ${h} * * *`;
    }
    case "weekly": {
      const days = orderDays(s.weekdays);
      if (days.length === 0) return null;
      if (days.length === 7) {
        const { h, m } = parseTime(s.time);
        return `${m} ${h} * * *`; // every day — no point listing all 7
      }
      const { h, m } = parseTime(s.time);
      return `${m} ${h} * * ${days.join(",")}`;
    }
    case "hourly": {
      const m = Math.min(59, Math.max(0, Math.round(s.minute)));
      return `${m} * * * *`;
    }
    case "everyNHours": {
      const n = Math.min(23, Math.max(1, Math.round(s.everyN)));
      return `0 */${n} * * *`;
    }
    case "advanced":
      return s.raw.trim() || null;
  }
}

const DAY_LABEL = new Map(WEEKDAYS.map((d) => [d.key, d]));

function describeDays(field: string): string | null {
  if (field === "*") return "every day";
  // Only handle comma lists of known names (what we emit); ranges/numbers
  // fall through to the raw description.
  const parts = field.split(",");
  if (!parts.every((p) => DAY_LABEL.has(p))) return null;
  const ordered = orderDays(parts);
  const keys = ordered.join(",");
  if (keys === "mon,tue,wed,thu,fri") return "every weekday";
  if (keys === "sat,sun") return "on weekends";
  const labels = ordered.map((k) => DAY_LABEL.get(k)!.short);
  return `on ${labels.join(", ")}`;
}

function hhmm(h: number, m: number): string {
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

/** Plain-English description of a cron expression, or the raw string when it
 *  doesn't match one of the friendly shapes. */
export function describeCron(cron: string): string {
  const f = cron.trim().split(/\s+/);
  if (f.length !== 5) return cron;
  const [min, hour, dom, mon, dow] = f;
  const numeric = (v: string) => (/^\d+$/.test(v) ? parseInt(v, 10) : null);

  // Every N hours: "0 */N * * *"
  const everyN = hour.match(/^\*\/(\d+)$/);
  if (min === "0" && everyN && dom === "*" && mon === "*" && dow === "*") {
    return `Every ${everyN[1]} hours`;
  }

  const m = numeric(min);
  // Hourly: "M * * * *"
  if (m !== null && hour === "*" && dom === "*" && mon === "*" && dow === "*") {
    return m === 0 ? "Every hour" : `Every hour at :${String(m).padStart(2, "0")}`;
  }

  const h = numeric(hour);
  if (m !== null && h !== null && dom === "*" && mon === "*") {
    if (dow === "*") return `Every day at ${hhmm(h, m)}`;
    const days = describeDays(dow);
    if (days) return `${days[0].toUpperCase()}${days.slice(1)} at ${hhmm(h, m)}`;
  }

  return cron; // unrecognised — show the expression itself
}
