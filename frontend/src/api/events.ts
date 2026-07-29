import { apiGet } from "./client";

export interface ServerEvent {
  id: number;
  server_id: string;
  created_at: string; // UTC, no timezone suffix (append "Z" before parsing)
  kind: string;
  message: string;
}

export interface ServerEventWithName extends ServerEvent {
  server_name: string;
}

export const getServerEvents = (serverId: string, limit = 30) =>
  apiGet<ServerEvent[]>(`/api/servers/${serverId}/events?limit=${limit}`);

export const getRecentEvents = (limit = 30) =>
  apiGet<ServerEventWithName[]>(`/api/events?limit=${limit}`);
