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
