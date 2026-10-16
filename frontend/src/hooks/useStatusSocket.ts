import { useEffect, useState } from "react";
import type { Server, ServerStatus, StatusFeedMessage } from "../api/servers";

// App-wide status feed (WS /ws/status). App owns the single socket; the
// sidebar, dashboard and an open server page all follow its map. Same-origin;
// Vite proxies /ws in dev, production serves it directly.
export function statusFeedUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/ws/status`;
}

export type StatusMap = Record<string, ServerStatus>;

/** Apply one feed message. A snapshot replaces the map (dropping ids that no
 *  longer exist); an update merges one key. Returns the same object when
 *  nothing changed so React can skip the re-render. */
export function reduceStatuses(map: StatusMap, msg: StatusFeedMessage): StatusMap {
  if (msg.type === "snapshot") {
    const next: StatusMap = {};
    for (const e of msg.servers) next[e.server_id] = e.status;
    const keys = Object.keys(next);
    const same =
      keys.length === Object.keys(map).length && keys.every((k) => map[k] === next[k]);
    return same ? map : next;
  }
  if (msg.type === "status") {
    if (map[msg.server_id] === msg.status) return map;
    return { ...map, [msg.server_id]: msg.status };
  }
  return map;
}

/** Overlay live statuses on a server list. Returns the same array when no row
 *  differs; otherwise only the changed rows are new objects. Ids missing from
 *  the list are ignored (a reload picks up new servers). */
export function patchServerStatuses<T extends Server>(servers: T[], statuses: StatusMap): T[] {
  let changed = false;
  const next = servers.map((s) => {
    const live = statuses[s.id];
    if (live === undefined || live === s.status) return s;
    changed = true;
    return { ...s, status: live };
  });
  return changed ? next : servers;
}

const BACKOFF_START = 1000;
const BACKOFF_MAX = 10000;

export function useStatusSocket(): { statuses: StatusMap; connected: boolean } {
  const [statuses, setStatuses] = useState<StatusMap>({});
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let disposed = false;
    let ws: WebSocket | null = null;
    let timer: number | undefined;
    let delay = BACKOFF_START;

    const connect = () => {
      if (disposed) return;
      ws = new WebSocket(statusFeedUrl());
      ws.onopen = () => {
        delay = BACKOFF_START;
        setConnected(true);
      };
      ws.onmessage = (event) => {
        let msg: StatusFeedMessage;
        try {
          msg = JSON.parse(event.data as string) as StatusFeedMessage;
        } catch {
          return;
        }
        setStatuses((prev) => reduceStatuses(prev, msg));
      };
      ws.onclose = () => {
        setConnected(false);
        // A dead feed must not override fresher REST data; the snapshot on
        // reconnect refills the map.
        setStatuses({});
        if (disposed) return;
        timer = window.setTimeout(connect, delay);
        delay = Math.min(delay * 2, BACKOFF_MAX);
      };
    };
    connect();

    return () => {
      disposed = true;
      window.clearTimeout(timer);
      if (ws) {
        ws.onopen = ws.onclose = ws.onmessage = null;
        ws.close();
      }
    };
  }, []);

  return { statuses, connected };
}
