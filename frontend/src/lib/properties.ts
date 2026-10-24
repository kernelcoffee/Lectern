// Pure helpers for the Properties tab's server.properties form.

import type { PropertiesResponse } from "../api/servers";

/**
 * Keys to render, in order: the union of keys in the file and known
 * definitions (so a fresh minimal file still offers difficulty/gamemode/etc.),
 * known keys first, alphabetical inside each group. Managed keys
 * (management-server-*) only appear once Lectern has written them, so a
 * server without the management protocol shows no empty read-only rows.
 */
export function propertyRows(data: PropertiesResponse): string[] {
  const keys = new Set([...Object.keys(data.definitions), ...Object.keys(data.properties)]);
  return [...keys]
    .filter((key) => !(data.definitions[key]?.managed && !(key in data.properties)))
    .sort((a, b) => {
      const aKnown = a in data.definitions ? 0 : 1;
      const bKnown = b in data.definitions ? 0 : 1;
      return aKnown - bKnown || a.localeCompare(b);
    });
}

/** How a saved key took effect: through the management protocol right away
 *  ("live") or written to the file for the next start ("file"). */
export type SaveOutcome = "live" | "file";

/** Per saved key, whether the PATCH applied it live (listed in the response's
 *  `applied_live`) or wrote it to the file. Keys not saved are absent. */
export function saveOutcome(
  savedKeys: string[],
  appliedLive: string[],
): Record<string, SaveOutcome> {
  const live = new Set(appliedLive);
  return Object.fromEntries(
    savedKeys.map((key): [string, SaveOutcome] => [key, live.has(key) ? "live" : "file"]),
  );
}

/** Live keys whose protocol setter only changes the default for players who
 *  have never joined (`gamemode` without force-gamemode): "now" alone would
 *  mislead, since everyone online keeps their own mode. With force-gamemode
 *  on, the same setter switches everyone online, so the plain wording holds. */
const NEW_PLAYERS_ONLY = new Set(["gamemode"]);

/**
 * The "when does this take effect" suffix for one server.properties row.
 * An unsaved edit describes what saving will do (live keys apply now only
 * while the management client is connected); otherwise the row reports how
 * its last save in this session took effect; untouched rows get no hint.
 */
export function applyHint(input: {
  edited: boolean;
  live: boolean;
  connected: boolean;
  lastSave: SaveOutcome | null;
  /** Row key; narrows the live wording for keys that only affect new players. */
  key?: string;
  /** `force-gamemode` is on: a live gamemode change applies to everyone online. */
  forceGamemode?: boolean;
}): string | null {
  const narrow = input.key && NEW_PLAYERS_ONLY.has(input.key) && !input.forceGamemode;
  const scope = narrow ? " to new players" : "";
  if (input.edited && input.live && input.connected) return `applies now${scope}`;
  if (input.edited) return "applies at next start";
  if (input.lastSave === "live") return `applied now${scope}`;
  if (input.lastSave === "file") return "applies at next start";
  return null;
}

/** Success toast after a server.properties save. */
export function savedMessage(appliedLive: string[]): string {
  const n = appliedLive.length;
  if (n === 0) return "server.properties saved.";
  return `server.properties saved — ${n} setting${n === 1 ? "" : "s"} applied now.`;
}

/**
 * The edits the file does not hold yet: those whose value differs from
 * `properties` (a key absent from the file reads as ""). After a failed PATCH
 * that still wrote part of the request (the 504 for an unconfirmed live set),
 * the rest are settled and must stop showing as unsaved.
 */
export function pendingEdits(
  edits: Record<string, string>,
  properties: Record<string, string>,
): Record<string, string> {
  return Object.fromEntries(
    Object.entries(edits).filter(([key, value]) => (properties[key] ?? "") !== value),
  );
}
