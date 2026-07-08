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
  /** Force the kind — "datapack" installs a project's datapack build. */
  kind?: "datapack";
}

export interface ContentUpdate {
  item_id: string;
  name: string;
  installed_version: string | null;
  new_version_id: string;
  new_version_number: string;
}

export type SortIndex = "relevance" | "downloads" | "follows" | "newest" | "updated";

export const searchContent = (params: {
  query: string;
  project_type?: string; // "mod" (default) | "resourcepack" | "datapack"
  loader?: string; // omit for loaderless kinds (resource packs, datapacks)
  mc_version: string;
  categories?: string[]; // Modrinth category slugs (AND-ed)
  index?: SortIndex;
  limit?: number;
  offset?: number;
}) => {
  const qs = new URLSearchParams({
    query: params.query,
    project_type: params.project_type ?? "mod",
    mc_version: params.mc_version,
    index: params.index ?? "relevance",
    limit: String(params.limit ?? 20),
    offset: String(params.offset ?? 0),
  });
  if (params.loader) qs.set("loader", params.loader);
  if (params.categories?.length) qs.set("categories", params.categories.join(","));
  return apiGet<SearchResponse>(`/api/content/search?${qs}`);
};

/** Modrinth category tags for one project type (name + header grouping). */
export const getModrinthCategories = (projectType: string) =>
  apiGet<{ name: string; header: string }[]>(
    `/api/content/categories?project_type=${projectType}`,
  );

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

// --- M7: resource packs -------------------------------------------------------

/** VT category payload (nested: a category holds packs and/or subcategories). */
export interface VtPack {
  name: string;
  display: string;
  description?: string;
}

export interface VtCategory {
  category: string;
  packs?: VtPack[];
  categories?: VtCategory[];
}

export type VtPackType = "resourcepacks" | "datapacks" | "craftingtweaks";

export const getVtCategories = (serverId: string, packType: VtPackType) =>
  apiGet<{ categories: VtCategory[] }>(
    `/api/servers/${serverId}/vanillatweaks/categories/${packType}`,
  );

/** Generate + install a VT selection (share code or explicit packs). A share
 * code carries its own pack type. Datapack sets return one item per pack. */
export const installVanillaTweaks = (
  serverId: string,
  body: {
    share_code?: string;
    packs?: Record<string, string[]>;
    pack_type?: VtPackType;
  },
) => apiPost<ContentItem[]>(`/api/servers/${serverId}/vanillatweaks`, body);

export const uploadResourcePack = async (
  serverId: string,
  file: File,
  kind: "resourcepack" | "datapack" = "resourcepack",
): Promise<ContentItem> => {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(
    `/api/servers/${serverId}/content/upload?kind=${kind}`,
    {
      method: "POST",
      body: form,
    },
  );
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(
      typeof body?.detail === "string" ? body.detail : `HTTP ${res.status}`,
    );
  }
  return (await res.json()) as ContentItem;
};

/** Point (or stop pointing) the server's resource-pack prompt at this item. */
export const serveResourcePack = (
  serverId: string,
  itemId: string,
  enabled: boolean,
) =>
  apiPost<{ resource_pack: string | null; resource_pack_sha1: string | null }>(
    `/api/servers/${serverId}/content/${itemId}/serve`,
    { enabled },
  );
