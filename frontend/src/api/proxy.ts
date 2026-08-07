// Proxy linking (Velocity servers): read the linked-backend registry and
// replace it. Order of server_ids = Velocity's try order (first entry is
// where new players land).

import { apiGet, apiPut } from "./client";

export interface ProxyLink {
  name: string;
  address: string;
  server_id: string | null; // null = an entry Lectern doesn't recognize
}

export interface ProxyCandidate {
  server_id: string;
  name: string;
  port: number;
  type: string;
  linked: boolean;
  /** How player-identity forwarding can work on this backend:
   *  "mod" = auto-installed, "manual" = needs a mod, "none" = impossible. */
  forwarding: "mod" | "manual" | "none";
}

export interface MovedPort {
  server_id: string;
  name: string;
  from: number;
  to: number;
}

export interface ProxyPayload {
  links: ProxyLink[];
  try: string[];
  candidates: ProxyCandidate[];
  warnings?: string[];
  /** Backends whose port collided with the proxy's and were reassigned. */
  moved?: MovedPort[];
}

export const getProxyLinks = (serverId: string) =>
  apiGet<ProxyPayload>(`/api/servers/${serverId}/proxy`);

export const setProxyLinks = (serverId: string, serverIds: string[]) =>
  apiPut<ProxyPayload>(`/api/servers/${serverId}/proxy`, {
    server_ids: serverIds,
  });
