// Server detail page — the hub for one server.
//
// Page anatomy (top to bottom):
//   status chip + name → meta line → EULA gate (until accepted) →
//   start/stop/restart/kill controls → StatsBar (only while running) →
//   tab bar (Console | Properties) → active tab.
//
// Data flow:
//   * `server` (ServerDetail) is fetched once on mount and refreshed after
//     every action; while status is a transition ("starting"/"stopping") we
//     poll every 1.5 s so the chip follows the process without a WebSocket.
//   * The Console tab has its own WebSocket (useConsoleSocket) — independent
//     of this polling.
//   * StatsBar polls GET /stats on its own 5 s timer; mounting it only when
//     status === "running" is what starts/stops that polling.
//   * PropertiesTab reports saved settings back via onServerUpdate so the
//     meta line (port/memory) updates without a refetch.
//
// Tabs are plain conditional rendering (no router): `tab` state picks which
// component is mounted. Console unmounts while on Properties, dropping its
// WebSocket; history replays from the server buffer when it remounts.

import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage } from "../../api/client";
import { getServerEvents } from "../../api/events";
import { STATUS_CHIP } from "../../components/status";
import { useToast } from "../../components/Toasts";
import { useInstallProgress } from "../../hooks/useInstallProgress";
import {
  acceptEula,
  cloneServer,
  deleteServer,
  getServer,
  serverAction,
  ServerAction,
  ServerDetail as ServerDetailType,
  ServerStatus,
  updateServerSettings,
} from "../../api/servers";
import BackupsTab from "./BackupsTab";
import ChangeVersionCard from "./ChangeVersionCard";
import Console from "./Console";
import DatapacksTab from "./DatapacksTab";
import FilesTab from "./FilesTab";
import ModsTab from "./ModsTab";
import MonitorPanel from "./MonitorPanel";
import PlayersTab from "./PlayersTab";
import PropertiesTab from "./PropertiesTab";
import ProxyTab from "./ProxyTab";
import ResourcePacksTab from "./ResourcePacksTab";
import ScheduleTab from "./ScheduleTab";
import StatsBar from "./StatsBar";

type Tab =
  | "console"
  | "metrics"
  | "proxy"
  | "players"
  | "mods"
  | "resourcepacks"
  | "datapacks"
  | "backups"
  | "schedule"
  | "files"
  | "version"
  | "properties";

// Server types that load mods — the Mods tab only renders for these.
const MODDED_TYPES: ServerDetailType["type"][] = ["fabric", "quilt", "neoforge", "forge", "paper"];

// Which controls are enabled for a given status.
function controlsFor(status: ServerStatus) {
  const running = status === "running" || status === "starting" || status === "stopping";
  return {
    canStart: status === "stopped" || status === "crashed",
    canStop: status === "running" || status === "starting",
    canRestart: running,
    canKill: running,
  };
}

export default function ServerDetail({
  serverId,
  onBack,
  onDeleted,
  onChanged,
  onOpen,
}: {
  serverId: string;
  onBack: () => void;
  onDeleted: () => void;
  /** Called after a change other pages care about (rename) so the parent can
   *  refresh the shared server list — sidebar/dashboard names follow. */
  onChanged?: () => void;
  /** Navigate to another server (used after cloning). */
  onOpen?: (id: string) => void;
}) {
  const [server, setServer] = useState<ServerDetailType | null>(null);
  // Load failures only — action failures go through toasts.
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<ServerAction | "eula" | "delete" | "clone" | null>(null);
  const [tab, setTab] = useState<Tab>("console");
  const toast = useToast();
  // Live pipeline step while installing (WS; null once done/not installing).
  const installProgress = useInstallProgress(
    server?.status === "installing" ? serverId : null,
  );
  // Why the server crashed (exit code / signal), from the event timeline.
  const [crashReason, setCrashReason] = useState<string | null>(null);

  useEffect(() => {
    if (server?.status !== "crashed") {
      setCrashReason(null);
      return;
    }
    let live = true;
    getServerEvents(serverId, 10)
      .then((events) => {
        const crash = events.find((e) => e.kind === "crashed");
        if (live) setCrashReason(crash?.message || null);
      })
      .catch(() => {
        // The banner is best-effort — the console still has the story.
      });
    return () => {
      live = false;
    };
  }, [server?.status, serverId]);

  const refresh = useCallback(async () => {
    try {
      setServer(await getServer(serverId));
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, [serverId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // The WS's terminal event is the moment the record flips (stopped or
  // install_failed) — refetch so the page swaps to the real controls.
  useEffect(() => {
    if (installProgress?.done || installProgress?.error) refresh();
  }, [installProgress?.done, installProgress?.error, refresh]);

  // Poll while a transition is in flight so the status chip follows the process.
  useEffect(() => {
    if (!server) return;
    const transient =
      server.status === "starting" || server.status === "stopping";
    if (!transient) return;
    const t = window.setInterval(refresh, 1500);
    return () => window.clearInterval(t);
  }, [server, refresh]);

  async function doAction(action: ServerAction) {
    setBusy(action);
    try {
      setServer(await serverAction(serverId, action));
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  async function doAcceptEula() {
    setBusy("eula");
    try {
      setServer(await acceptEula(serverId));
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  async function doClone() {
    if (!server) return;
    setBusy("clone");
    try {
      const clone = await cloneServer(serverId, {});
      toast.success(`Cloned "${server.name}".`);
      onChanged?.();
      onOpen?.(clone.id);
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  async function doDelete() {
    if (
      !window.confirm(
        `Delete "${server?.name}"? This removes the server and all its files.`,
      )
    )
      return;
    setBusy("delete");
    try {
      await deleteServer(serverId);
      onDeleted();
    } catch (e) {
      toast.error(errorMessage(e));
      setBusy(null);
    }
  }

  if (!server) {
    return (
      <div className="p-4 sm:p-6 space-y-4">
        <button onClick={onBack} className="text-sm text-slate-400 hover:text-slate-200">
          ← Back
        </button>
        {error ? (
          <p className="text-sm text-red-400">{error}</p>
        ) : (
          <p className="text-sm text-slate-500">Loading…</p>
        )}
      </div>
    );
  }

  const c = controlsFor(server.status);
  const installing =
    server.status === "installing" || server.status === "install_failed";
  // Proxies aren't Minecraft servers: no players/content/version tabs.
  const isProxy = server.type === "velocity";

  return (
    <div className="p-4 sm:p-6 space-y-5">
      <button onClick={onBack} className="text-sm text-slate-400 hover:text-slate-200">
        ← Dashboard
      </button>

      {/* Header — identity + controls on the left, live-stats card top-right. */}
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-start">
        <div className="space-y-2.5">
          <div className="flex flex-wrap items-center gap-3">
            <span
              className={`text-xs px-2 py-0.5 rounded-full ${STATUS_CHIP[server.status]}`}
            >
              {server.status}
            </span>
            <EditableName
              name={server.name}
              onRename={async (name) => {
                setServer(await updateServerSettings(serverId, { name }));
                onChanged?.();
              }}
              onError={(msg) => toast.error(msg)}
            />
            <button
              onClick={doClone}
              disabled={busy !== null || server.running}
              title={server.running ? "Stop the server before cloning" : undefined}
              className="text-xs text-slate-400 hover:text-slate-200 disabled:opacity-50"
            >
              {busy === "clone" ? "Cloning…" : "Clone"}
            </button>
            <button
              onClick={doDelete}
              disabled={busy !== null}
              className="text-xs text-red-400 hover:text-red-300 disabled:opacity-50"
            >
              {busy === "delete" ? "Deleting…" : "Delete server"}
            </button>
          </div>
          <p className="text-xs text-slate-400">
            {server.type} · MC {server.mc_version}
            {server.loader_version && ` · ${server.loader_version}`} · :{server.port} ·{" "}
            {server.memory_mb} MB
          </p>

          {/* Controls */}
          {!installing && (
            <div className="flex flex-wrap gap-2 pt-1">
              <ControlButton
                label="Start"
                color="emerald"
                disabled={!c.canStart || !server.eula_accepted || busy !== null}
                onClick={() => doAction("start")}
              />
              <ControlButton
                label="Stop"
                color="slate"
                disabled={!c.canStop || busy !== null}
                onClick={() => doAction("stop")}
              />
              <ControlButton
                label="Restart"
                color="slate"
                disabled={!c.canRestart || busy !== null}
                onClick={() => doAction("restart")}
              />
              <ControlButton
                label="Kill"
                color="red"
                disabled={!c.canKill || busy !== null}
                onClick={() => doAction("kill")}
              />
            </div>
          )}
        </div>

        {/* Live stats card — mounted only while running (starts/stops polling). */}
        {server.status === "running" && <StatsBar serverId={serverId} />}
      </div>

      {/* EULA gate */}
      {!installing && !server.eula_accepted && (
        <div className="rounded-lg border border-amber-700/60 bg-amber-950/30 p-3 space-y-2">
          <p className="text-sm text-amber-200">
            You must accept the{" "}
            <a
              href="https://aka.ms/MinecraftEULA"
              target="_blank"
              rel="noreferrer"
              className="underline"
            >
              Minecraft EULA
            </a>{" "}
            before starting this server.
          </p>
          <button
            onClick={doAcceptEula}
            disabled={busy === "eula"}
            className="bg-amber-600 hover:bg-amber-500 disabled:opacity-50 rounded px-3 py-1.5 text-sm font-medium text-slate-900"
          >
            {busy === "eula" ? "Accepting…" : "Accept EULA"}
          </button>
        </div>
      )}

      {server.status === "crashed" && crashReason && (
        <div className="rounded-lg border border-red-800/60 bg-red-950/30 p-3">
          <p className="text-sm text-red-200">
            <span className="font-medium">Crashed:</span> {crashReason}
          </p>
        </div>
      )}

      {installing &&
        (server.status === "install_failed" ? (
          <p className="text-sm text-red-400">
            Install failed
            {installProgress?.error ? ` — ${installProgress.error}` : ""}. Delete
            the server and try again.
          </p>
        ) : (
          <p className="text-sm text-sky-400">
            Server is still installing
            {installProgress ? ` — ${installProgress.message}` : ""}… controls
            appear once it is ready.
          </p>
        ))}

      {/* Tabs — plain conditional rendering, no router. */}
      {!installing && (
        <>
          {/* Scrolls horizontally on narrow screens — 11 tabs never fit a phone. */}
          <div className="flex gap-1 border-b border-slate-800 overflow-x-auto">
            <TabButton
              label="Console"
              active={tab === "console"}
              onClick={() => setTab("console")}
            />
            <TabButton
              label="Metrics"
              active={tab === "metrics"}
              onClick={() => setTab("metrics")}
            />
            {isProxy && (
              <TabButton
                label="Proxy"
                active={tab === "proxy"}
                onClick={() => setTab("proxy")}
              />
            )}
            {!isProxy && (
              <TabButton
                label="Players"
                active={tab === "players"}
                onClick={() => setTab("players")}
              />
            )}
            {MODDED_TYPES.includes(server.type) && (
              <TabButton
                label="Mods"
                active={tab === "mods"}
                onClick={() => setTab("mods")}
              />
            )}
            {!isProxy && (
              <TabButton
                label="Resource packs"
                active={tab === "resourcepacks"}
                onClick={() => setTab("resourcepacks")}
              />
            )}
            {!isProxy && (
              <TabButton
                label="Datapacks"
                active={tab === "datapacks"}
                onClick={() => setTab("datapacks")}
              />
            )}
            <TabButton
              label="Backups"
              active={tab === "backups"}
              onClick={() => setTab("backups")}
            />
            <TabButton
              label="Schedule"
              active={tab === "schedule"}
              onClick={() => setTab("schedule")}
            />
            <TabButton
              label="Files"
              active={tab === "files"}
              onClick={() => setTab("files")}
            />
            {!isProxy && (
              <TabButton
                label="Version"
                active={tab === "version"}
                onClick={() => setTab("version")}
              />
            )}
            <TabButton
              label="Properties"
              active={tab === "properties"}
              onClick={() => setTab("properties")}
            />
          </div>

          {tab === "console" && (
            <section className="space-y-2">
              <Console serverId={serverId} />
            </section>
          )}
          {tab === "mods" && <ModsTab serverId={serverId} server={server} />}
          {tab === "resourcepacks" && (
            <ResourcePacksTab serverId={serverId} server={server} />
          )}
          {tab === "datapacks" && (
            <DatapacksTab serverId={serverId} server={server} />
          )}
          {tab === "backups" && (
            <BackupsTab
              serverId={serverId}
              server={server}
              onServerChanged={refresh}
            />
          )}
          {tab === "metrics" && <MonitorPanel serverId={serverId} />}
          {tab === "proxy" && <ProxyTab serverId={serverId} />}
          {tab === "players" && <PlayersTab serverId={serverId} />}
          {tab === "schedule" && <ScheduleTab serverId={serverId} />}
          {tab === "files" && <FilesTab serverId={serverId} />}
          {tab === "version" && (
            <ChangeVersionCard server={server} onServerUpdate={setServer} />
          )}
          {tab === "properties" && (
            <PropertiesTab
              serverId={serverId}
              server={server}
              onServerUpdate={setServer}
            />
          )}
        </>
      )}
    </div>
  );
}

// Click-to-edit server name: pencil → input; Enter/blur saves, Escape cancels.
// Save goes through PATCH /api/servers/{id} (name conflicts 409 → onError).
function EditableName({
  name,
  onRename,
  onError,
}: {
  name: string;
  onRename: (name: string) => Promise<void>;
  onError: (message: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(name);
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) inputRef.current?.select();
  }, [editing]);

  async function save() {
    const trimmed = draft.trim();
    if (saving) return;
    if (!trimmed || trimmed === name) {
      setEditing(false);
      setDraft(name);
      return;
    }
    setSaving(true);
    try {
      await onRename(trimmed);
      setEditing(false);
    } catch (e) {
      onError(errorMessage(e));
      inputRef.current?.select(); // keep editing so the user can fix it
    } finally {
      setSaving(false);
    }
  }

  if (!editing) {
    return (
      <div className="flex items-center gap-2 flex-1 min-w-0">
        <h2 className="text-xl font-semibold truncate">{name}</h2>
        <button
          onClick={() => {
            setDraft(name);
            setEditing(true);
          }}
          title="Rename server"
          aria-label="Rename server"
          className="text-slate-500 hover:text-slate-200 shrink-0"
        >
          <svg viewBox="0 0 20 20" fill="currentColor" className="h-4 w-4">
            <path d="M13.586 2.586a2 2 0 0 1 2.828 2.828l-.793.793-2.828-2.828.793-.793ZM11.379 4.793 3 13.172V16h2.828l8.38-8.379-2.83-2.828Z" />
          </svg>
        </button>
      </div>
    );
  }

  return (
    <input
      ref={inputRef}
      value={draft}
      disabled={saving}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={save}
      onKeyDown={(e) => {
        if (e.key === "Enter") e.currentTarget.blur(); // blur triggers save
        if (e.key === "Escape") {
          setDraft(name); // reset so blur's save() sees no change
          setEditing(false);
        }
      }}
      className="flex-1 min-w-0 rounded bg-slate-800 px-2 py-1 text-xl font-semibold text-slate-100 outline-none ring-1 ring-slate-600 focus:ring-emerald-500 disabled:opacity-50"
    />
  );
}

function TabButton({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={
        "px-3 sm:px-4 py-2 text-sm rounded-t border-b-2 -mb-px whitespace-nowrap shrink-0 " +
        (active
          ? "border-emerald-500 text-slate-100"
          : "border-transparent text-slate-400 hover:text-slate-200")
      }
    >
      {label}
    </button>
  );
}

function ControlButton({
  label,
  color,
  disabled,
  onClick,
}: {
  label: string;
  color: "emerald" | "slate" | "red";
  disabled: boolean;
  onClick: () => void;
}) {
  const styles: Record<string, string> = {
    emerald: "bg-emerald-600 hover:bg-emerald-500",
    slate: "bg-slate-700 hover:bg-slate-600",
    red: "bg-red-700 hover:bg-red-600",
  };
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`${styles[color]} disabled:opacity-40 disabled:cursor-not-allowed rounded px-4 py-1.5 text-sm font-medium`}
    >
      {label}
    </button>
  );
}
