// App shell — Crafty-inspired layout: fixed left sidebar (dashboard link +
// server list + "new server"), main content on the right.
//
// Routing stays state-based (no router dep): `route` picks the page. The
// server list lives here because the sidebar and every page share it —
// children mutate through `reload` and navigate through `go`. While any
// server is installing we poll the list so sidebar + dashboard follow the
// install without a manual refresh.

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "./api/client";
import { listServers, Server } from "./api/servers";
import Sidebar from "./components/Sidebar";
import CreateServer from "./pages/CreateServer";
import Dashboard from "./pages/Dashboard";
import Players from "./pages/Players";
import ServerDetail from "./pages/ServerDetail";
import Settings from "./pages/Settings";

type Health = { status: string; version: string };

export type Route =
  | { view: "dashboard" }
  | { view: "create" }
  | { view: "players" }
  | { view: "settings" }
  | { view: "server"; id: string };

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState(false);
  const [route, setRoute] = useState<Route>({ view: "dashboard" });
  const [servers, setServers] = useState<Server[]>([]);

  useEffect(() => {
    apiGet<Health>("/api/health")
      .then(setHealth)
      .catch(() => setHealthError(true));
  }, []);

  const reload = useCallback(async () => {
    try {
      setServers(await listServers());
    } catch {
      // Backend unreachable — the header badge already says so.
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  // Follow installs live: poll while any server is still provisioning.
  const anyInstalling = servers.some((s) => s.status === "installing");
  useEffect(() => {
    if (!anyInstalling) return;
    const t = window.setInterval(reload, 2000);
    return () => window.clearInterval(t);
  }, [anyInstalling, reload]);

  const go = useCallback((r: Route) => setRoute(r), []);

  return (
    <div className="min-h-screen bg-slate-900 text-slate-100 flex">
      <Sidebar
        servers={servers}
        route={route}
        onNavigate={go}
        backend={
          health ? `v${health.version}` : healthError ? "offline" : "connecting…"
        }
        backendOk={health !== null}
      />

      <main className="flex-1 min-w-0 overflow-y-auto h-screen">
        {route.view === "dashboard" && (
          <Dashboard servers={servers} onReload={reload} onNavigate={go} />
        )}
        {route.view === "players" && <Players />}
        {route.view === "settings" && <Settings />}
        {route.view === "create" && (
          <CreateServer
            onCreated={async () => {
              await reload();
              go({ view: "dashboard" });
            }}
          />
        )}
        {route.view === "server" && (
          <ServerDetail
            key={route.id}
            serverId={route.id}
            onBack={() => go({ view: "dashboard" })}
            onDeleted={async () => {
              await reload();
              go({ view: "dashboard" });
            }}
            onChanged={reload}
            onOpen={async (id) => {
              await reload();
              go({ view: "server", id });
            }}
          />
        )}
      </main>
    </div>
  );
}
