import { apiDelete, apiGet, apiPatch, apiPost } from "./client";

export type ScheduleAction = "start" | "stop" | "restart" | "backup" | "command";

export interface Schedule {
  id: string;
  server_id: string;
  action: ScheduleAction;
  cron: string;
  command: string | null;
  one_time: boolean;
  enabled: boolean;
  /** Next firing (ISO, local server time); null while disabled. */
  next_run: string | null;
}

export interface ScheduleCreate {
  action: ScheduleAction;
  cron: string;
  command?: string | null;
  one_time?: boolean;
  enabled?: boolean;
}

export interface ScheduleUpdate {
  action?: ScheduleAction;
  cron?: string;
  command?: string | null;
  one_time?: boolean;
  enabled?: boolean;
}

export const listSchedules = (serverId: string) =>
  apiGet<Schedule[]>(`/api/servers/${serverId}/schedules`);

export const createSchedule = (serverId: string, body: ScheduleCreate) =>
  apiPost<Schedule>(`/api/servers/${serverId}/schedules`, body);

export const updateSchedule = (
  serverId: string,
  scheduleId: string,
  body: ScheduleUpdate,
) => apiPatch<Schedule>(`/api/servers/${serverId}/schedules/${scheduleId}`, body);

export const deleteSchedule = (serverId: string, scheduleId: string) =>
  apiDelete(`/api/servers/${serverId}/schedules/${scheduleId}`);
