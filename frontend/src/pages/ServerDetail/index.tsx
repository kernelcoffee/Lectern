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

import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../../api/client";
import {
  acceptEula,
  deleteServer,
  getServer,
  serverAction,
  ServerAction,
  ServerDetail as ServerDetailType,
  ServerStatus,
} from "../../api/servers";
import BackupsTab from "./BackupsTab";
import Console from "./Console";
import DatapacksTab from "./DatapacksTab";
import ModsTab from "./ModsTab";
import PropertiesTab from "./PropertiesTab";
import ResourcePacksTab from "./ResourcePacksTab";
import StatsBar from "./StatsBar";

type Tab = "console" | "mods" | "resourcepacks" | "datapacks" | "backups" | "properties";

// Server types that load mods — the Mods tab only renders for these.
const MODDED_TYPES: ServerDetailType["type"][] = ["fabric", "quilt", "paper"];

const STATUS_STYLES: Record<ServerStatus, string> = {
  installing: "bg-sky-500 text-slate-900",
  install_failed: "bg-red-500 text-slate-100",
  stopped: "bg-slate-600 text-slate-100",
  starting: "bg-amber-500 text-slate-900",
  running: "bg-emerald-500 text-slate-900",
  stopping: "bg-amber-500 text-slate-900",
  crashed: "bg-red-500 text-slate-100",
};

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
}: {
  serverId: string;
  onBack: () => void;
  onDeleted: () => void;
}) {
  const [server, setServer] = useState<ServerDetailType | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<ServerAction | "eula" | "delete" | null>(null);
  const [tab, setTab] = useState<Tab>("console");

  const refresh = useCallback(async () => {
    try {
      setServer(await getServer(serverId));
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, [serverId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

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
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function doAcceptEula() {
    setBusy("eula");
    try {
      setServer(await acceptEula(serverId));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
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
      setError(e instanceof ApiError ? e.message : String(e));
      setBusy(null);
    }
  }

  if (!server) {
    return (
      <div className="p-6 space-y-4">
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

  return (
    <div className="p-6 space-y-5">
      <button onClick={onBack} className="text-sm text-slate-400 hover:text-slate-200">
        ← Dashboard
      </button>

      <div className="flex items-center gap-3">
        <span
          className={`text-xs px-2 py-0.5 rounded-full ${STATUS_STYLES[server.status]}`}
        >
          {server.status}
        </span>
        <h2 className="text-xl font-semibold flex-1 truncate">{server.name}</h2>
        <button
          onClick={doDelete}
          disabled={busy !== null}
          className="text-xs text-red-400 hover:text-red-300 disabled:opacity-50"
        >
          {busy === "delete" ? "Deleting…" : "Delete server"}
        </button>
      </div>
      <p className="text-xs text-slate-400 -mt-3">
        {server.type} · MC {server.mc_version}
        {server.loader_version && ` · ${server.loader_version}`} · :{server.port} ·{" "}
        {server.memory_mb} MB
      </p>

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

      {installing && (
        <p className="text-sm text-sky-400">
          Server is still installing — controls appear once it is ready.
        </p>
      )}

      {/* Controls */}
      {!installing && (
        <div className="flex flex-wrap gap-2">
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

      {/* Live stats — mounted only while running, which is also what starts
          and stops the underlying 5s polling (see StatsBar). */}
      {server.status === "running" && <StatsBar serverId={serverId} />}

      {/* Tabs — plain conditional rendering, no router. */}
      {!installing && (
        <>
          <div className="flex gap-1 border-b border-slate-800">
            <TabButton
              label="Console"
              active={tab === "console"}
              onClick={() => setTab("console")}
            />
            {MODDED_TYPES.includes(server.type) && (
              <TabButton
                label="Mods"
                active={tab === "mods"}
                onClick={() => setTab("mods")}
              />
            )}
            <TabButton
              label="Resource packs"
              active={tab === "resourcepacks"}
              onClick={() => setTab("resourcepacks")}
            />
            <TabButton
              label="Datapacks"
              active={tab === "datapacks"}
              onClick={() => setTab("datapacks")}
            />
            <TabButton
              label="Backups"
              active={tab === "backups"}
              onClick={() => setTab("backups")}
            />
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
          {tab === "properties" && (
            <PropertiesTab
              serverId={serverId}
              server={server}
              onServerUpdate={setServer}
            />
          )}
        </>
      )}

      {error && <p className="text-sm text-red-400">{error}</p>}
    </div>
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
        "px-4 py-2 text-sm rounded-t border-b-2 -mb-px " +
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
