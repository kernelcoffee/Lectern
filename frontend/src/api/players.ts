import { apiDelete, apiDeleteJson, apiGet, apiPost } from "./client";

export interface Player {
  uuid: string;
  name: string;
  added_at: string;
}

export interface PlayerEntry {
  uuid: string;
  name: string;
}

export type ListKind = "whitelist" | "ops" | "banned";

export interface PlayerLists {
  whitelist: PlayerEntry[];
  ops: PlayerEntry[];
  banned: PlayerEntry[];
}

// --- global registry --------------------------------------------------------

export const listPlayers = () => apiGet<Player[]>("/api/players");

export const addPlayer = (query: string) =>
  apiPost<Player>("/api/players", { query });

export const removePlayer = (uuid: string) =>
  apiDelete(`/api/players/${uuid}`);

// --- per-server lists -------------------------------------------------------

export const getPlayerLists = (serverId: string) =>
  apiGet<PlayerLists>(`/api/servers/${serverId}/playerlists`);

export const addToList = (serverId: string, kind: ListKind, uuid: string) =>
  apiPost<PlayerLists>(`/api/servers/${serverId}/playerlists/${kind}`, { uuid });

export const removeFromList = (serverId: string, kind: ListKind, uuid: string) =>
  apiDeleteJson<PlayerLists>(
    `/api/servers/${serverId}/playerlists/${kind}/${uuid}`,
  );

/** Player head avatar (Crafatar). Decorative — falls back gracefully offline. */
export const avatarUrl = (uuid: string, size = 32) =>
  `https://crafatar.com/avatars/${uuid}?size=${size}&overlay`;
