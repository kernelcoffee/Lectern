import { apiGet } from "./client";

export interface ServerTypeInfo {
  key: string;
  needs_loader: boolean;
}

export const getServerTypes = () =>
  apiGet<ServerTypeInfo[]>("/api/catalog/server-types");

export const getMinecraftVersions = (type: string) =>
  apiGet<string[]>(
    `/api/catalog/minecraft-versions?type=${encodeURIComponent(type)}`,
  );

export const getFabricLoaders = (mcVersion: string) =>
  apiGet<string[]>(`/api/catalog/loaders/fabric/${encodeURIComponent(mcVersion)}`);
