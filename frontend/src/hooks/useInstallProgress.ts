// Live install progress over WebSocket (replaces the old 5s polling of
// GET /{id}/progress). The backend sends the current snapshot on connect,
// then every update, and closes the socket itself after a terminal event
// (done or error) — so this hook needs no polling and no reconnect logic.

import { useEffect, useState } from "react";

export interface InstallProgress {
  server_id: string;
  step: string;
  message: string;
  done: boolean;
  error: string | null;
}

export function installProgressUrl(serverId: string): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/ws/servers/${serverId}/install`;
}

/** Latest install progress for a server, or null before the first event.
 *  Pass null to disable (no socket is opened). */
export function useInstallProgress(
  serverId: string | null,
): InstallProgress | null {
  const [progress, setProgress] = useState<InstallProgress | null>(null);

  useEffect(() => {
    setProgress(null);
    if (!serverId) return;
    const ws = new WebSocket(installProgressUrl(serverId));
    ws.onmessage = (event) =>
      setProgress(JSON.parse(event.data as string) as InstallProgress);
    return () => {
      ws.onmessage = null;
      ws.close();
    };
  }, [serverId]);

  return progress;
}
