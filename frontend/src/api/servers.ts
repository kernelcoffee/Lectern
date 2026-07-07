import { apiDelete, apiGet, apiPost } from "./client";

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
