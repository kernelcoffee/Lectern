// M9 — backups API. Create runs inline (seconds at LAN scale); restore
// requires a stopped server and fully replaces the server directory. Upload
// adopts a whole-server zip as a backup ("uploaded" trigger).

import { apiDelete, apiGet, apiPost, apiUpload } from "./client";

export interface Backup {
  id: string;
  server_id: string;
  filename: string;
  size_bytes: number;
  created_at: string;
  trigger: "manual" | "scheduled" | "uploaded";
}

export const listBackups = (serverId: string) =>
  apiGet<Backup[]>(`/api/servers/${serverId}/backups`);

export const createBackup = (serverId: string) =>
  apiPost<Backup>(`/api/servers/${serverId}/backups`, {});

/** Upload a whole-server zip (e.g. a downloaded backup edited offline) as a
 * new backup. ``onProgress`` reports the fraction of the body sent; the
 * backend then validates the archive before responding. */
export const uploadBackup = (
  serverId: string,
  file: File,
  onProgress?: (fraction: number) => void,
) => {
  const form = new FormData();
  form.append("file", file);
  return apiUpload<Backup>(`/api/servers/${serverId}/backups/upload`, form, onProgress);
};

export const restoreBackup = (serverId: string, backupId: string) =>
  apiPost<void>(`/api/servers/${serverId}/backups/${backupId}/restore`, {});

export const deleteBackup = (serverId: string, backupId: string) =>
  apiDelete(`/api/servers/${serverId}/backups/${backupId}`);

/** Direct URL to download a backup archive (used as an <a href>). */
export const backupDownloadUrl = (serverId: string, backupId: string) =>
  `/api/servers/${serverId}/backups/${backupId}/download`;
