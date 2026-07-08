// M6 — content (mods) API.
//
// Data flow overview:
//   * searchContent      ← GET /api/content/search — Modrinth search, always
//     called with the server's loader + MC version so results are compatible.
//   * listContent        ← GET /api/servers/{id}/content — installed items.
//   * installContent     → POST /api/servers/{id}/content — installs the
//     project and its required dependencies (optional ones on request);
//     returns every item added/replaced so the UI can show what came along.
//   * checkUpdates       ← GET /api/servers/{id}/content/updates
//   * applyUpdate        → POST /api/servers/{id}/content/{item}/update
//   * patchContent       → PATCH (enable/disable, release channel)
//   * removeContent      → DELETE
//
// Mod changes take effect at the next server start (same model as
// server.properties).

import { apiDelete, apiGet, apiPatch, apiPost } from "./client";

export type ReleaseChannel = "release" | "beta" | "alpha";

/** One Modrinth search hit (subset of fields we render). */
export interface SearchHit {
  project_id: string;
  slug: string;
  title: string;
  description: string;
  downloads: number;
  icon_url: string | null;
  categories: string[];
}

export interface SearchResponse {
  hits: SearchHit[];
  total_hits: number;
}

/** One installed content item (mirrors the server-side manifest entry). */
export interface ContentItem {
  id: string;
  server_id: string;
  kind: string; // "mod" | (later) "resourcepack" | "plugin"
  source: string; // "modrinth"
  project_id: string | null;
  version_id: string | null;
  version_number: string | null;
  slug: string | null;
  name: string;
  filename: string;
  sha512: string | null;
  channel: ReleaseChannel;
  enabled: boolean;
  installed_at: string;
}

export interface ContentInstallRequest {
  project_id: string; // Modrinth id or slug
  version_id?: string; // omit → newest allowed by channel
  channel?: ReleaseChannel;
  include_optional_deps?: boolean;
}

export interface ContentUpdate {
  item_id: string;
  name: string;
  installed_version: string | null;
  new_version_id: string;
  new_version_number: string;
}

export const searchContent = (params: {
  query: string;
  loader: string;
  mc_version: string;
  offset?: number;
}) => {
  const qs = new URLSearchParams({
    query: params.query,
    loader: params.loader,
    mc_version: params.mc_version,
    offset: String(params.offset ?? 0),
  });
  return apiGet<SearchResponse>(`/api/content/search?${qs}`);
};

export const listContent = (serverId: string) =>
  apiGet<ContentItem[]>(`/api/servers/${serverId}/content`);

export const installContent = (serverId: string, body: ContentInstallRequest) =>
  apiPost<ContentItem[]>(`/api/servers/${serverId}/content`, body);

export const checkContentUpdates = (serverId: string) =>
  apiGet<ContentUpdate[]>(`/api/servers/${serverId}/content/updates`);

export const applyContentUpdate = (serverId: string, itemId: string) =>
  apiPost<ContentItem>(`/api/servers/${serverId}/content/${itemId}/update`, {});

export const patchContent = (
  serverId: string,
  itemId: string,
  body: { enabled?: boolean; channel?: ReleaseChannel },
) => apiPatch<ContentItem>(`/api/servers/${serverId}/content/${itemId}`, body);

export const removeContent = (serverId: string, itemId: string) =>
  apiDelete(`/api/servers/${serverId}/content/${itemId}`);
