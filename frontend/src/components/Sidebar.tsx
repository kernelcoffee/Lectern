// Left navigation column (Crafty-style): brand, Dashboard, the server list
// with a "New server" entry, and a backend-health footer.
//
// Each server row shows a status dot; the dot is supplementary (title tooltip
// carries the word, and the dashboard/detail chips spell it out) so state is
// never color-alone anywhere it matters.

import { Route } from "../App";
import { Server } from "../api/servers";
import { STATUS_DOT } from "./status";

export default function Sidebar({
  servers,
  route,
  onNavigate,
  open,
  onClose,
  backend,
  backendOk,
}: {
  servers: Server[];
  route: Route;
  onNavigate: (r: Route) => void;
  /** Mobile drawer state — ignored from md up, where the sidebar is static. */
  open: boolean;
  onClose: () => void;
  backend: string;
  backendOk: boolean;
}) {
  return (
    <>
      {/* Mobile backdrop */}
      {open && (
        <div
          className="fixed inset-0 z-40 bg-black/60 md:hidden"
          onClick={onClose}
          aria-hidden
        />
      )}
      <aside
        className={
          "fixed inset-y-0 left-0 z-50 w-60 flex flex-col bg-slate-950 border-r border-slate-800 " +
          "transform transition-transform duration-200 md:static md:h-dvh md:shrink-0 md:translate-x-0 md:transition-none " +
          (open ? "translate-x-0" : "-translate-x-full")
        }
      >
      {/* Brand */}
      <button
        onClick={() => onNavigate({ view: "dashboard" })}
        className="flex items-center gap-2.5 px-5 py-4 text-left border-b border-slate-800/60"
      >
        <img src="/lectern.svg" alt="" className="h-7 w-7 shrink-0" />
        <span>
          <h1 className="text-lg font-semibold tracking-tight">Lectern</h1>
          <p className="text-[11px] text-slate-500">Minecraft server manager</p>
        </span>
      </button>

      <nav className="flex-1 overflow-y-auto py-3 space-y-6">
        <ul>
          <NavItem
            label="Dashboard"
            icon={<GridIcon />}
            active={route.view === "dashboard"}
            onClick={() => onNavigate({ view: "dashboard" })}
          />
          <NavItem
            label="Players"
            icon={<UsersIcon />}
            active={route.view === "players"}
            onClick={() => onNavigate({ view: "players" })}
          />
        </ul>

        {/* Proxies get their own section — they front the game servers, so
            mixing them into one list obscures the topology. */}
        {servers.some((s) => s.type === "velocity") && (
          <div>
            <p className="px-5 pb-1 text-[11px] font-medium uppercase tracking-wider text-slate-500">
              Proxies
            </p>
            <ul>
              {servers
                .filter((s) => s.type === "velocity")
                .map((s) => (
                  <NavItem
                    key={s.id}
                    label={s.name}
                    icon={
                      <span className="flex items-center gap-1.5 shrink-0">
                        <ProxyIcon />
                        <span
                          title={s.status}
                          className={`w-2 h-2 rounded-full shrink-0 ${STATUS_DOT[s.status]}`}
                        />
                      </span>
                    }
                    active={route.view === "server" && route.id === s.id}
                    onClick={() => onNavigate({ view: "server", id: s.id })}
                  />
                ))}
            </ul>
          </div>
        )}

        <div>
          <p className="px-5 pb-1 text-[11px] font-medium uppercase tracking-wider text-slate-500">
            Servers
          </p>
          <ul>
            {servers
              .filter((s) => s.type !== "velocity")
              .map((s) => (
                <NavItem
                  key={s.id}
                  label={s.name}
                  icon={
                    <span
                      title={s.status}
                      className={`w-2 h-2 rounded-full shrink-0 ${STATUS_DOT[s.status]}`}
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
    </>
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

function ProxyIcon() {
  // Fork/fan-out glyph: one inlet, several outlets — the proxy topology.
  return (
    <svg viewBox="0 0 16 16" className="w-3 h-3 fill-current shrink-0 text-slate-500">
      <path d="M7 1h2v5.2l4.5 3-1.1 1.6L8 7.9l-4.4 2.9-1.1-1.6L7 6.2Zm-5 12a2 2 0 1 1 4 0 2 2 0 0 1-4 0Zm8 0a2 2 0 1 1 4 0 2 2 0 0 1-4 0Z" />
    </svg>
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

function UsersIcon() {
  return (
    <svg viewBox="0 0 16 16" className="w-4 h-4 fill-current shrink-0">
      <path d="M6 8a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Zm0 1c-2.2 0-4 1.2-4 2.8V13h8v-1.2C10 10.2 8.2 9 6 9Zm5.5-1a2 2 0 1 0 0-4 2 2 0 0 0 0 4Zm.5 1c-.5 0-1 .1-1.4.3.9.7 1.4 1.6 1.4 2.5V13h3v-1.2C15 10.1 13.7 9 12 9Z" />
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
