import { useCallback, useEffect, useRef, useState } from "react";

// Same-origin WebSocket to the backend console. Vite proxies /ws in dev; in
// production the API serves it directly.
function consoleUrl(serverId: string): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/ws/servers/${serverId}/console`;
}

const MAX_LINES = 1000;

export interface ConsoleSocket {
  lines: string[];
  connected: boolean;
  send: (command: string) => void;
}

export function useConsoleSocket(serverId: string): ConsoleSocket {
  const [lines, setLines] = useState<string[]>([]);
  const [connected, setConnected] = useState(false);
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    setLines([]);
    const ws = new WebSocket(consoleUrl(serverId));
    socketRef.current = ws;

    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    ws.onmessage = (event) => {
      setLines((prev) => {
        const next = [...prev, event.data as string];
        return next.length > MAX_LINES ? next.slice(-MAX_LINES) : next;
      });
    };

    return () => {
      ws.onopen = ws.onclose = ws.onmessage = null;
      ws.close();
      socketRef.current = null;
    };
  }, [serverId]);

  const send = useCallback((command: string) => {
    const ws = socketRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(command);
  }, []);

  return { lines, connected, send };
}
