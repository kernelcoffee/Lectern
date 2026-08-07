import {
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
  apiPostForm,
  apiUpload,
} from "./client";

export type ServerType =
  | "vanilla"
  | "fabric"
  | "quilt"
  | "neoforge"
  | "forge"
  | "paper";
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
  auto_start_delay: number;
  crash_restart: boolean;
  stop_command: string;
  shutdown_timeout: number;
  log_retention_days: number;
  backup_excluded: string;
  backup_max: number;
  backup_compress: boolean;
  backup_stop_server: boolean;
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
  seed?: string;
  whitelist?: boolean;
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
  auto_start_delay?: number;
  crash_restart?: boolean;
  stop_command?: string;
  shutdown_timeout?: number;
  log_retention_days?: number;
  backup_excluded?: string;
  backup_max?: number;
  backup_compress?: boolean;
  backup_stop_server?: boolean;
}

/** Backend metadata for one well-known server.properties key. */
export interface PropertyDefinition {
  type: "boolean" | "integer" | "enum" | "string";
  values: string[] | null; // enum choices (only for type "enum")
  min: number | null; // integer bounds (only for type "integer")
  max: number | null;
  description: string;
  /** Vanilla's built-in default, shown next to unset fields (null = none). */
  default: string | null;
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
/** Next free name/port + configured default memory for the create form. */
export const suggestServerDefaults = () =>
  apiGet<{ name: string; port: number; memory_mb: number }>(
    "/api/servers/suggest",
  );
export const getServer = (id: string) =>
  apiGet<ServerDetail>(`/api/servers/${id}`);
export const createServer = (body: ServerCreate) =>
  apiPost<Server>("/api/servers", body);
export const deleteServer = (id: string) => apiDelete(`/api/servers/${id}`);
export const cloneServer = (
  id: string,
  body: { name?: string; port?: number; include_world?: boolean },
) => apiPost<Server>(`/api/servers/${id}/clone`, body);
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

// --- M11 — resource history + on-disk size ----------------------------------

export interface StatSample {
  created_at: string;
  cpu_percent: number;
  memory_mb: number;
  players_online: number;
}

export interface ServerSize {
  world_bytes: number | null;
  server_bytes: number | null;
  computed_at: string | null;
}

export const getStatsHistory = (id: string, minutes = 60) =>
  apiGet<StatSample[]>(`/api/servers/${id}/stats/history?minutes=${minutes}`);

export const getServerSize = (id: string) =>
  apiGet<ServerSize>(`/api/servers/${id}/size`);

// --- M9.5 — server version change -------------------------------------------

export interface VersionChangeRequest {
  mc_version: string;
  loader_version?: string | null;
  allow_downgrade?: boolean;
  backup_first?: boolean;
}

/** Names of installed content by outcome after a version change. */
export interface MigrationReport {
  updated: string[];
  incompatible: string[];
  regenerated: string[];
  kept: string[];
}

export interface VersionChangeResponse {
  server: ServerDetail;
  report: MigrationReport;
}

export const changeServerVersion = (id: string, body: VersionChangeRequest) =>
  apiPost<VersionChangeResponse>(`/api/servers/${id}/version`, body);

export interface WorldImportResult {
  server: ServerDetail;
  written: number;
  skipped: number;
}

/**
 * Import an existing world into a stopped, installed server from an uploaded
 * .zip or a download URL (used by the create wizard). Replaces its world.
 * `exclude` is comma-separated glob patterns for files to skip (defaults on the
 * server to Distant Horizons caches); pass "" to import everything.
 * `onProgress` reports upload fraction (0–1) for the file path.
 */
export const importWorld = (
  id: string,
  source: { file?: File; url?: string; exclude?: string },
  onProgress?: (fraction: number) => void,
) => {
  const form = new FormData();
  if (source.file) form.append("file", source.file);
  if (source.url) form.append("url", source.url);
  if (source.exclude !== undefined) form.append("exclude", source.exclude);
  return source.file
    ? apiUpload<WorldImportResult>(`/api/servers/${id}/world`, form, onProgress)
    : apiPostForm<WorldImportResult>(`/api/servers/${id}/world`, form);
};

/** Dry-run compatibility check: how installed content would fare on
 *  `mcVersion`. Read-only; safe to call while the server runs. */
export const previewVersionChange = (id: string, mcVersion: string) =>
  apiGet<MigrationReport>(
    `/api/servers/${id}/version/preview?mc_version=${encodeURIComponent(mcVersion)}`,
  );
