// Left navigation column (Crafty-style): brand, Dashboard, the server list
// with a "New server" entry, and a backend-health footer.
//
// Each server row shows a status dot; the dot is supplementary (title tooltip
// carries the word, and the dashboard/detail chips spell it out) so state is
// never color-alone anywhere it matters.

import { Route } from "../App";
import { Server, ServerStatus } from "../api/servers";

const DOT: Record<ServerStatus, string> = {
  installing: "bg-sky-400",
  install_failed: "bg-red-500",
  stopped: "bg-slate-500",
  starting: "bg-amber-400",
  running: "bg-emerald-400",
  stopping: "bg-amber-400",
  crashed: "bg-red-500",
};

export default function Sidebar({
  servers,
  route,
  onNavigate,
  backend,
  backendOk,
}: {
  servers: Server[];
  route: Route;
  onNavigate: (r: Route) => void;
  backend: string;
  backendOk: boolean;
}) {
  return (
    <aside className="w-60 shrink-0 h-screen sticky top-0 flex flex-col bg-slate-950 border-r border-slate-800">
      {/* Brand */}
      <button
        onClick={() => onNavigate({ view: "dashboard" })}
        className="px-5 py-4 text-left border-b border-slate-800/60"
      >
        <h1 className="text-lg font-semibold tracking-tight">Lectern</h1>
        <p className="text-[11px] text-slate-500">Minecraft server manager</p>
      </button>

      <nav className="flex-1 overflow-y-auto py-3 space-y-6">
        <ul>
          <NavItem
            label="Dashboard"
            icon={<GridIcon />}
            active={route.view === "dashboard"}
            onClick={() => onNavigate({ view: "dashboard" })}
          />
        </ul>

        <div>
          <p className="px-5 pb-1 text-[11px] font-medium uppercase tracking-wider text-slate-500">
            Servers
          </p>
          <ul>
            {servers.map((s) => (
              <NavItem
                key={s.id}
                label={s.name}
                icon={
                  <span
                    title={s.status}
                    className={`w-2 h-2 rounded-full shrink-0 ${DOT[s.status]}`}
                  />
                }
                active={route.view === "server" && route.id === s.id}
                onClick={() => onNavigate({ view: "server", id: s.id })}
              />
            ))}
            <NavItem
              label="New server"
              icon={<PlusIcon />}
              active={route.view === "create"}
              onClick={() => onNavigate({ view: "create" })}
              muted
            />
          </ul>
        </div>
      </nav>

      <div className="border-t border-slate-800/60 py-2">
        <ul>
          <NavItem
            label="Settings"
            icon={<GearIcon />}
            active={route.view === "settings"}
            onClick={() => onNavigate({ view: "settings" })}
            muted
          />
        </ul>
      </div>

      <footer className="px-5 py-3 border-t border-slate-800/60 text-[11px]">
        <span className={backendOk ? "text-emerald-400" : "text-slate-500"}>
          backend {backend}
        </span>
      </footer>
    </aside>
  );
}

function NavItem({
  label,
  icon,
  active,
  onClick,
  muted,
}: {
  label: string;
  icon: React.ReactNode;
  active: boolean;
  onClick: () => void;
  muted?: boolean;
}) {
  return (
    <li>
      <button
        onClick={onClick}
        className={
          "w-full flex items-center gap-2.5 px-5 py-2 text-sm text-left truncate border-l-2 " +
          (active
            ? "border-emerald-500 bg-slate-900 text-slate-100"
            : `border-transparent hover:bg-slate-900/60 ${
                muted ? "text-slate-500 hover:text-slate-300" : "text-slate-300"
              }`)
        }
      >
        {icon}
        <span className="truncate">{label}</span>
      </button>
    </li>
  );
}

function GridIcon() {
  return (
    <svg viewBox="0 0 16 16" className="w-4 h-4 fill-current shrink-0">
      <path d="M1 1h6v6H1zM9 1h6v6H9zM1 9h6v6H1zM9 9h6v6H9z" />
    </svg>
  );
}

function PlusIcon() {
  return (
    <svg viewBox="0 0 16 16" className="w-4 h-4 fill-current shrink-0">
      <path d="M7 2h2v5h5v2H9v5H7V9H2V7h5z" />
    </svg>
  );
}

function GearIcon() {
  return (
    <svg viewBox="0 0 24 24" className="w-4 h-4 fill-current shrink-0">
      <path d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Zm0 2a2 2 0 1 1 0 4 2 2 0 0 1 0-4Zm7.4 3.5.9 1.6-1.9 3.3-1.8-.5a6.9 6.9 0 0 1-1.4.8l-.4 1.8h-3.6l-.4-1.8a6.9 6.9 0 0 1-1.4-.8l-1.8.5L3.7 15l1.3-1.3a7 7 0 0 1 0-1.6L3.7 10.8l1.9-3.3 1.8.5c.4-.3.9-.6 1.4-.8l.4-1.8h3.6l.4 1.8c.5.2 1 .5 1.4.8l1.8-.5 1.9 3.3-1.3 1.3c.1.5.1 1.1 0 1.6Z" />
    </svg>
  );
}
