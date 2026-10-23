import type { MsmpState } from "../api/servers";

// One name for the feature everywhere in the UI: "management protocol".
// The schema version is the protocol's own (rpc.discover), not a Minecraft or
// Lectern version, so it only ever appears labelled "schema".

/** Server-page chip for the management-protocol client, or null for none. */
export interface MsmpChip {
  connected: boolean;
  label: string;
  title: string;
}

/**
 * Chip for a server's management-protocol client. `null`/absent means the
 * server has no client (not running, proxy, or below 1.21.9): no chip.
 */
export function msmpChip(msmp: MsmpState | null | undefined): MsmpChip | null {
  if (!msmp) return null;
  if (msmp.connected) {
    const schema = msmp.schema_version ? ` (schema ${msmp.schema_version})` : "";
    return {
      connected: true,
      label: "management protocol",
      title:
        `Minecraft Server Management Protocol connected${schema}. ` +
        "The online roster and kicks go through it.",
    };
  }
  return {
    connected: false,
    label: "management protocol connecting…",
    title:
      "Minecraft Server Management Protocol not connected. " +
      "The online roster and kicks use the server console until it connects.",
  };
}

/** Players-tab subtitle naming the roster source that is active right now. */
export function rosterSource(msmp: MsmpState | null | undefined): string {
  return msmp?.connected
    ? "live via the management protocol — includes Carpet-style bots"
    : "live from the server console — includes Carpet-style bots";
}

/** Refetch interval while the client is connecting: catch the connect quickly. */
export const MSMP_PENDING_POLL_MS = 5000;
/** Refetch interval once connected: notice a later drop to the console fallback. */
export const MSMP_CONNECTED_POLL_MS = 15000;

/**
 * How often the server page refetches its row to keep the management-protocol
 * chip and the Players-tab source honest, or null for no polling. A client
 * exists only while running; it can connect late (1.21.9–26.1 open the port
 * after startup) and can drop and reconnect later, all without a status
 * change, so both states are polled.
 */
export function msmpPollInterval(
  status: string | undefined,
  msmp: MsmpState | null | undefined,
): number | null {
  if (status !== "running" || !msmp) return null;
  return msmp.connected ? MSMP_CONNECTED_POLL_MS : MSMP_PENDING_POLL_MS;
}
