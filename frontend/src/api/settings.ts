import { apiGet, apiPatch } from "./client";

export interface AppSetting {
  key: string;
  label: string;
  help: string;
  unit: string;
  value: number;
  min: number;
  max: number;
  category: string;
}

export const getSettings = () => apiGet<AppSetting[]>("/api/settings");

export const updateSettings = (updates: Record<string, number>) =>
  apiPatch<AppSetting[]>("/api/settings", updates);
