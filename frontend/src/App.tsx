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
  // Mobile-only nav drawer (the sidebar is static from md up).
  const [navOpen, setNavOpen] = useState(false);

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

  const go = useCallback((r: Route) => {
    setRoute(r);
    setNavOpen(false); // navigating from the drawer closes it
  }, []);

  return (
    <div className="min-h-dvh bg-slate-900 text-slate-100 md:flex">
      {/* Mobile top bar — the sidebar is off-canvas below md. */}
      <header className="sticky top-0 z-30 flex items-center gap-3 border-b border-slate-800 bg-slate-950 px-4 py-3 md:hidden">
        <button
          onClick={() => setNavOpen(true)}
          aria-label="Open navigation"
          className="text-slate-300 hover:text-slate-100"
        >
          <svg viewBox="0 0 16 16" className="w-5 h-5 fill-current">
            <path d="M1 3h14v2H1zM1 7h14v2H1zM1 11h14v2H1z" />
          </svg>
        </button>
        <button
          onClick={() => go({ view: "dashboard" })}
          className="flex items-center gap-2 text-base font-semibold tracking-tight"
        >
          <img src="/lectern.svg" alt="" className="h-6 w-6" />
          Lectern
        </button>
      </header>

      <Sidebar
        servers={servers}
        route={route}
        onNavigate={go}
        open={navOpen}
        onClose={() => setNavOpen(false)}
        backend={
          health ? `v${health.version}` : healthError ? "offline" : "connecting…"
        }
        backendOk={health !== null}
      />

      <main className="flex-1 min-w-0 md:h-dvh md:overflow-y-auto">
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
