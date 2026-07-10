// M12 — file manager API. Every path is server-relative ("" = server root);
// the backend confines all of them to the server directory.

import { apiDelete, apiGet, apiPost, apiPut, apiUpload } from "./client";

export interface FileEntry {
  name: string;
  is_dir: boolean;
  size: number;
  mtime: number; // epoch seconds
  mode: string; // unix permission string, e.g. "-rw-r--r--"
}

export interface DirListing {
  path: string;
  entries: FileEntry[];
}

export interface FileContent {
  path: string;
  content: string | null; // null when binary or too large
  binary: boolean;
  too_large: boolean;
  size: number;
}

const q = (path: string) => `path=${encodeURIComponent(path)}`;

export const listFiles = (serverId: string, path = "") =>
  apiGet<DirListing>(`/api/servers/${serverId}/files?${q(path)}`);

export const readFile = (serverId: string, path: string) =>
  apiGet<FileContent>(`/api/servers/${serverId}/files/content?${q(path)}`);

export const writeFile = (serverId: string, path: string, content: string) =>
  apiPut<void>(`/api/servers/${serverId}/files/content?${q(path)}`, { content });

export const makeDir = (serverId: string, path: string) =>
  apiPost<{ path: string }>(`/api/servers/${serverId}/files/dir`, { path });

export const renamePath = (serverId: string, path: string, to: string) =>
  apiPost<void>(`/api/servers/${serverId}/files/rename`, { path, to });

export const deletePath = (serverId: string, path: string) =>
  apiDelete(`/api/servers/${serverId}/files?${q(path)}`);

export const fileDownloadUrl = (serverId: string, path: string) =>
  `/api/servers/${serverId}/files/download?${q(path)}`;

export const uploadFile = (
  serverId: string,
  dirPath: string,
  file: File,
  onProgress?: (f: number) => void,
) => {
  const form = new FormData();
  form.append("file", file);
  form.append("path", dirPath);
  return apiUpload<{ name: string; size: number }>(
    `/api/servers/${serverId}/files/upload`,
    form,
    onProgress,
  );
};
