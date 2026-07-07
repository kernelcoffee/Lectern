import { apiDelete, apiGet, apiPatch, apiPost } from "./client";

export type ServerType = "vanilla" | "fabric" | "quilt" | "paper";
export type ServerStatus =
  | "installing"
  | "install_failed"
  | "stopped"
  | "starting"
  | "running"
  | "stopping"
  | "crashed";

export interface InstallProgress {
  server_id: string;
  step: string;
  message: string;
  done: boolean;
  error: string | null;
}

export interface Server {
  id: string;
  name: string;
  type: ServerType;
  mc_version: string;
  loader_version: string | null;
  port: number;
  memory_mb: number;
  status: ServerStatus;
  created_at: string;
}

export interface ServerDetail extends Server {
  jvm_args: string;
  auto_start: boolean;
  crash_restart: boolean;
  stop_command: string;
  shutdown_timeout: number;
  eula_accepted: boolean;
  running: boolean;
}

export type ServerAction = "start" | "stop" | "restart" | "kill";

export interface ServerCreate {
  name: string;
  type: ServerType;
  mc_version: string;
  loader_version?: string | null;
  port: number;
  memory_mb: number;
}

// ---------------------------------------------------------------------------
// M5 — configuration & stats types.
//
// Data flow overview:
//   * ServerSettingsUpdate  → PATCH /api/servers/{id}          (DB-backed knobs)
//   * PropertiesResponse    ← GET/PATCH /api/servers/{id}/properties
//     `properties` is the raw server.properties key→value map (all strings, as
//     in the file); `definitions` is backend metadata describing well-known
//     keys (type/enum values/min/max) that PropertiesTab uses to pick a widget.
//   * ServerStats           ← GET /api/servers/{id}/stats — polled by StatsBar
//     while the server is running; `ping` is null until the server has fully
//     booted and answers a Server List Ping.
// ---------------------------------------------------------------------------

/** Partial update of Lectern-owned settings; omit fields you don't change. */
export interface ServerSettingsUpdate {
  name?: string;
  port?: number;
  memory_mb?: number;
  jvm_args?: string;
  auto_start?: boolean;
  crash_restart?: boolean;
  stop_command?: string;
  shutdown_timeout?: number;
}

/** Backend metadata for one well-known server.properties key. */
export interface PropertyDefinition {
  type: "boolean" | "integer" | "enum" | "string";
  values: string[] | null; // enum choices (only for type "enum")
  min: number | null; // integer bounds (only for type "integer")
  max: number | null;
  description: string;
}

export interface PropertiesResponse {
  properties: Record<string, string>;
  definitions: Record<string, PropertyDefinition>;
}

/** What the Minecraft server reports over a Server List Ping. */
export interface PingInfo {
  online: number;
  max: number;
  players: string[]; // sample of online player names
  motd: string;
  version: string;
  favicon: string | null; // "data:image/png;base64,…" or null
}

export interface ServerStats {
  running: boolean;
  pid: number | null;
  uptime_seconds: number | null;
  cpu_percent: number | null;
  memory_mb: number | null;
  ping: PingInfo | null;
}

export const listServers = () => apiGet<Server[]>("/api/servers");
export const getServer = (id: string) =>
  apiGet<ServerDetail>(`/api/servers/${id}`);
export const createServer = (body: ServerCreate) =>
  apiPost<Server>("/api/servers", body);
export const deleteServer = (id: string) => apiDelete(`/api/servers/${id}`);
export const getServerProgress = (id: string) =>
  apiGet<InstallProgress>(`/api/servers/${id}/progress`);
export const serverAction = (id: string, action: ServerAction) =>
  apiPost<ServerDetail>(`/api/servers/${id}/action/${action}`, {});
export const acceptEula = (id: string) =>
  apiPost<ServerDetail>(`/api/servers/${id}/eula`, {});

// --- M5 endpoints -----------------------------------------------------------

export const updateServerSettings = (id: string, body: ServerSettingsUpdate) =>
  apiPatch<ServerDetail>(`/api/servers/${id}`, body);

export const getProperties = (id: string) =>
  apiGet<PropertiesResponse>(`/api/servers/${id}/properties`);

/**
 * Merge updates into server.properties. Values may be typed (boolean/number);
 * the backend validates and stores them as strings. `null` removes a key.
 */
export const patchProperties = (
  id: string,
  updates: Record<string, string | number | boolean | null>,
) => apiPatch<PropertiesResponse>(`/api/servers/${id}/properties`, updates);

export const getServerStats = (id: string) =>
  apiGet<ServerStats>(`/api/servers/${id}/stats`);
