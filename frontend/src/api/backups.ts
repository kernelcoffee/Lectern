// M9 — backups API. Create runs inline (seconds at LAN scale); restore
// requires a stopped server and fully replaces the server directory.

import { apiDelete, apiGet, apiPost } from "./client";

export interface Backup {
  id: string;
  server_id: string;
  filename: string;
  size_bytes: number;
  created_at: string;
  trigger: "manual" | "scheduled";
}

export const listBackups = (serverId: string) =>
  apiGet<Backup[]>(`/api/servers/${serverId}/backups`);

export const createBackup = (serverId: string) =>
  apiPost<Backup>(`/api/servers/${serverId}/backups`, {});

export const restoreBackup = (serverId: string, backupId: string) =>
  apiPost<void>(`/api/servers/${serverId}/backups/${backupId}/restore`, {});

export const deleteBackup = (serverId: string, backupId: string) =>
  apiDelete(`/api/servers/${serverId}/backups/${backupId}`);
